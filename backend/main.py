from fastapi import FastAPI, HTTPException, UploadFile, File, Form # Adicionado UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, Response # Adicionado Response
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles  # Adicione esta linha junto aos outros imports
from typing import Optional # Importante para os campos não obrigatórios
import sqlite3
from datetime import date
import io
import qrcode
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import os
import shutil # Para salvar a nota fiscal
from PIL import Image
import csv

app = FastAPI(title="Statiot Inventory API")

import os
os.makedirs("notas_fiscais", exist_ok=True)

app.mount("/notas_fiscais", StaticFiles(directory="notas_fiscais"), name="notas_fiscais")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "estoque.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS itens (
            codigo TEXT PRIMARY KEY,
            nome TEXT NOT NULL,
            estoque_minimo INTEGER NOT NULL,
            localizacao TEXT DEFAULT 'Geral',
            responsavel TEXT DEFAULT NULL
        )
    ''')




    # Migrações de segurança caso as colunas novas não existam em bancos antigos
    try:
        cursor.execute("ALTER TABLE itens ADD COLUMN localizacao TEXT DEFAULT 'Geral'")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE itens ADD COLUMN responsavel TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass

    # 1. Cria a tabela de movimentações primeiro
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS movimentacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            codigo_item TEXT NOT NULL,
            tipo TEXT CHECK(tipo IN ('Entrada', 'Saída')) NOT NULL,
            quantidade INTEGER NOT NULL,
            FOREIGN KEY (codigo_item) REFERENCES itens (codigo)
        )
    ''')

    # 2. Depois faz a migração da coluna usuario com segurança
    try:
        cursor.execute("ALTER TABLE movimentacoes ADD COLUMN usuario TEXT DEFAULT 'Sistema'")
    except sqlite3.OperationalError:
        pass  # A coluna já existe

    # Dados iniciais de itens
    cursor.execute("INSERT OR IGNORE INTO itens (codigo, nome, estoque_minimo, localizacao, responsavel) VALUES ('M8x30', 'Parafuso sextavado M8x30', 200, 'Prateleira A - Setor 02', NULL)")
    cursor.execute("INSERT OR IGNORE INTO itens (codigo, nome, estoque_minimo, localizacao, responsavel) VALUES ('CH3', 'Chapa aço carbono 3mm', 10, 'Almoxarifado Central', NULL)")
    cursor.execute("INSERT OR IGNORE INTO itens (codigo, nome, estoque_minimo, localizacao, responsavel) VALUES ('NB-01', 'Notebook Dell Latitude', 1, 'TI - Sala de Suporte', 'Carlos Silva')")

    cursor.execute("SELECT COUNT(*) FROM movimentacoes")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO movimentacoes (data, codigo_item, tipo, quantidade) VALUES ('2026-08-17', 'M8x30', 'Entrada', 500)")
        cursor.execute("INSERT INTO movimentacoes (data, codigo_item, tipo, quantidade) VALUES ('2026-08-18', 'M8x30', 'Saída', 120)")
        cursor.execute("INSERT INTO movimentacoes (data, codigo_item, tipo, quantidade) VALUES ('2026-08-18', 'CH3', 'Entrada', 20)")
        cursor.execute("INSERT INTO movimentacoes (data, codigo_item, tipo, quantidade) VALUES ('2026-08-20', 'NB-01', 'Entrada', 1)")


# Novas colunas (Financeiro e Garantia) - Opcionais
    novas_colunas = [
        ("valor_unitario", "REAL DEFAULT 0.0"),
        ("fornecedor", "TEXT DEFAULT NULL"),
        ("data_compra", "TEXT DEFAULT NULL"),
        ("validade_garantia", "TEXT DEFAULT NULL"),
        ("nota_fiscal", "TEXT DEFAULT NULL"),
        ("caminho_nf", "TEXT DEFAULT NULL") # Para guardar onde o arquivo PDF/Imagem foi salvo
    ]
    
    for coluna, tipo in novas_colunas:
        try:
            cursor.execute(f"ALTER TABLE itens ADD COLUMN {coluna} {tipo}")
        except sqlite3.OperationalError:
            pass # Coluna já existe

    # Cria a pasta para guardar as notas fiscais, se não existir
    os.makedirs("notas_fiscais", exist_ok=True)
    
    conn.commit()
    conn.close()

# Inicializa o banco de itens e movimentações
init_db()

# Atualize o modelo do Admin para barrar valores negativos no backend
class ItemAdminUpdate(BaseModel):
    nome: str
    estoque_minimo: int
    localizacao: str
    responsavel: Optional[str] = None
    valor_unitario: float = Field(default=0.0, ge=0.0) # <--- Impede negativo
    fornecedor: Optional[str] = None
    data_compra: Optional[str] = None
    validade_garantia: Optional[str] = None
    nota_fiscal: Optional[str] = None

# O ItemCreate original continua simples para o operador não travar
class ItemCreate(BaseModel):
    codigo: str
    nome: str
    estoque_minimo: int
    localizacao: str = "Geral"
    responsavel: Optional[str] = None

# Novo modelo completo para o Admin
class ItemAdminUpdate(BaseModel):
    nome: str
    estoque_minimo: int
    localizacao: str
    responsavel: Optional[str] = None
    valor_unitario: float = 0.0
    fornecedor: Optional[str] = None
    data_compra: Optional[str] = None
    validade_garantia: Optional[str] = None
    nota_fiscal: Optional[str] = None

class MovimentacaoCreate(BaseModel):
    codigo_item: str
    tipo: str
    quantidade: int = Field(..., gt=0)
    usuario: str = "Sistema"  # Novo campo adicionado

class ItemUpdate(BaseModel):
    nome: str
    estoque_minimo: int
    localizacao: str
    responsavel: str | None = None

class UsuarioCreate(BaseModel):
    nome: str
    email: str 
    senha: str
    role: str  # 'admin', 'operador' ou 'viewer'

class LoginPayload(BaseModel):
    email: str
    senha: str


# --- GESTÃO DE USUÁRIOS E CRIAÇÃO DA TABELA ---
def criar_tabela_usuarios():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha TEXT NOT NULL,
            role TEXT NOT NULL
        )
    """)
    # Cria usuários padrão caso a tabela esteja vazia
    cursor.execute("SELECT COUNT(*) FROM usuarios")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO usuarios (nome, email, senha, role) VALUES (?, ?, ?, ?)",
                       ("Administrador", "admin@statiot.com", "admin123", "admin"))
        cursor.execute("INSERT INTO usuarios (nome, email, senha, role) VALUES (?, ?, ?, ?)",
                       ("Operador Padrão", "operador@statiot.com", "op123", "operador"))
        cursor.execute("INSERT INTO usuarios (nome, email, senha, role) VALUES (?, ?, ?, ?)",
                       ("Visualizador", "viewer@statiot.com", "view123", "viewer"))
        conn.commit()
    conn.close()

criar_tabela_usuarios()


# --- ROTAS DA API ---

@app.get("/api/estoque")
def get_estoque():
    conn = get_db_connection()
    query = '''
        SELECT 
            i.nome AS Item, i.codigo AS Codigo, i.localizacao AS Localizacao, 
            i.responsavel AS Responsavel, i.valor_unitario, i.fornecedor, 
            i.data_compra, i.caminho_nf,
            COALESCE(SUM(CASE WHEN m.tipo = 'Entrada' THEN m.quantidade ELSE 0 END), 0) - 
            COALESCE(SUM(CASE WHEN m.tipo = 'Saída' THEN m.quantidade ELSE 0 END), 0) AS Saldo,
            i.estoque_minimo AS EstoqueMinimo
        FROM itens i
        LEFT JOIN movimentacoes m ON i.codigo = m.codigo_item
        GROUP BY i.codigo, i.nome, i.estoque_minimo, i.localizacao, i.responsavel, i.valor_unitario, i.fornecedor, i.data_compra, i.caminho_nf;
    '''
    cursor = conn.cursor()
    cursor.execute(query)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.get("/api/historico/{codigo}")
def get_historico_item(codigo: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    # Adicionado o campo 'usuario' no SELECT
    cursor.execute("SELECT data, tipo, quantidade, usuario FROM movimentacoes WHERE codigo_item = ? ORDER BY id DESC", (codigo,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.post("/api/itens")
def criar_item(item: ItemCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO itens (codigo, nome, estoque_minimo, localizacao, responsavel) VALUES (?, ?, ?, ?, ?)",
            (item.codigo, item.nome, item.estoque_minimo, item.localizacao, item.responsavel)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Este código de item já existe.")
    conn.close()
    return {"mensagem": "Item cadastrado com sucesso!", "codigo": item.codigo}

@app.post("/api/movimentacoes")
def criar_movimentacao(mov: MovimentacaoCreate):
    if mov.tipo not in ["Entrada", "Saída"]:
        raise HTTPException(status_code=400, detail="Tipo deve ser 'Entrada' ou 'Saída'.")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verifica se o item existe
    codigo_limpo = mov.codigo_item.strip()
    cursor.execute("SELECT codigo FROM itens WHERE LOWER(codigo) = LOWER(?)", (codigo_limpo,))
    item_encontrado = cursor.fetchone()
    
    if not item_encontrado:
        conn.close()
        raise HTTPException(status_code=404, detail="Item não encontrado no cadastro.")
    
    codigo_real = item_encontrado["codigo"]
    
    # Bloqueia saída se não houver saldo suficiente
    if mov.tipo == "Saída":
        cursor.execute('''
            SELECT 
                COALESCE(SUM(CASE WHEN tipo = 'Entrada' THEN quantidade ELSE 0 END), 0) - 
                COALESCE(SUM(CASE WHEN tipo = 'Saída' THEN quantidade ELSE 0 END), 0) AS saldo
            FROM movimentacoes
            WHERE codigo_item = ?
        ''', (codigo_real,))
        
        resultado = cursor.fetchone()
        saldo_atual = resultado["saldo"] if resultado else 0
        
        if mov.quantidade > saldo_atual:
            conn.close()
            raise HTTPException(
                status_code=400, 
                detail=f"Saldo insuficiente! Você tem {saldo_atual} unidades no estoque, mas tentou retirar {mov.quantidade}."
            )
    
    # Registra a movimentação com a data, quantidades e o USUÁRIO
    data_atual = str(date.today())
    cursor.execute(
        "INSERT INTO movimentacoes (data, codigo_item, tipo, quantidade, usuario) VALUES (?, ?, ?, ?, ?)",
        (data_atual, codigo_real, mov.tipo, mov.quantidade, mov.usuario)
    )
    conn.commit()
    conn.close()
    return {"mensagem": "Movimentação registrada com sucesso!"}

@app.get("/api/etiqueta/{codigo}")
def gerar_etiqueta_pdf(codigo: str):
    conn = get_db_connection()
    item = conn.execute("SELECT * FROM itens WHERE codigo = ?", (codigo,)).fetchone()
    conn.close()
    
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado")
    
    nome_item = item["nome"] if "nome" in item.keys() else "Item de Estoque"
    url_ativo = f"https://luigicalima-hash.github.io/estoque-statiot/ativo.html?codigo={codigo}"
    
    qr = qrcode.QRCode(version=1, box_size=10, border=1)
    qr.add_data(url_ativo)
    qr.make(fit=True)
    img_qr = qr.make_image(fill_color="black", back_color="white")
    
    qr_buffer = io.BytesIO()
    img_qr.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)

    pdf_buffer = io.BytesIO()
    largura_etiqueta = 80 * mm
    altura_etiqueta = 50 * mm
    
    c = canvas.Canvas(pdf_buffer, pagesize=(largura_etiqueta, altura_etiqueta))
    c.setLineWidth(0.5)
    c.rect(2 * mm, 2 * mm, largura_etiqueta - 4 * mm, altura_etiqueta - 4 * mm)
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(base_dir, "..", "img", "log-png.png")
    
    if os.path.exists(logo_path):
        try:
            c.drawImage(logo_path, 5 * mm, 38 * mm, width=14 * mm, height=7 * mm, preserveAspectRatio=True, mask='auto')
            c.setFont("Helvetica-Bold", 8.5)
            c.drawString(21 * mm, 41 * mm, "STATIOT — CONTROLE DE ATIVOS")
        except:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(5 * mm, 42 * mm, "STATIOT — CONTROLE DE ATIVOS")
    else:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(5 * mm, 42 * mm, "STATIOT — CONTROLE DE ATIVOS")
        
    c.line(5 * mm, 36 * mm, largura_etiqueta - 5 * mm, 36 * mm)
    c.drawImage(ImageReader(qr_buffer), 47 * mm, 8 * mm, width=28 * mm, height=28 * mm)
    
    c.setFont("Helvetica-Bold", 8)
    c.drawString(6 * mm, 30 * mm, "CÓDIGO / ATIVO:")
    c.setFont("Helvetica", 10)
    c.drawString(6 * mm, 25 * mm, codigo)
    
    c.setFont("Helvetica-Bold", 8)
    c.drawString(6 * mm, 19 * mm, "DESCRIÇÃO:")
    c.setFont("Helvetica", 9)
    c.drawString(6 * mm, 14 * mm, nome_item[:22])
    
    c.setFont("Helvetica-Bold", 6)
    c.setFillColorRGB(0.2, 0.2, 0.2)
    c.drawString(6 * mm, 6 * mm, "PROPRIEDADE STATIOT — USO INTERNO")

    c.showPage()
    c.save()
    pdf_buffer.seek(0)
    
    return StreamingResponse(
        pdf_buffer, 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename=etiqueta_{codigo}.pdf"}
    )


@app.put("/api/admin/itens/{codigo}")
def atualizar_item_completo(codigo: str, item: ItemAdminUpdate):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT codigo FROM itens WHERE LOWER(codigo) = LOWER(?)", (codigo,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    
    cursor.execute('''
        UPDATE itens SET 
            nome = ?, estoque_minimo = ?, localizacao = ?, responsavel = ?, 
            valor_unitario = ?, fornecedor = ?, data_compra = ?, 
            validade_garantia = ?, nota_fiscal = ?
        WHERE LOWER(codigo) = LOWER(?)
    ''', (
        item.nome, item.estoque_minimo, item.localizacao, item.responsavel, 
        item.valor_unitario, item.fornecedor, item.data_compra, 
        item.validade_garantia, item.nota_fiscal, codigo
    ))
    conn.commit()
    conn.close()
    return {"mensagem": "Ficha do item atualizada com sucesso!"}



@app.post("/api/admin/itens/{codigo}/upload-nf")
async def upload_nota_fiscal(codigo: str, arquivo: UploadFile = File(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT codigo FROM itens WHERE LOWER(codigo) = LOWER(?)", (codigo,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    
    # Salva o arquivo na pasta notas_fiscais
    extensao = arquivo.filename.split(".")[-1]
    nome_arquivo = f"NF_{codigo}.{extensao}"
    caminho_destino = os.path.join("notas_fiscais", nome_arquivo)
    
    with open(caminho_destino, "wb") as buffer:
        shutil.copyfileobj(arquivo.file, buffer)
        
    # Salva o caminho no banco de dados
    cursor.execute("UPDATE itens SET caminho_nf = ? WHERE LOWER(codigo) = LOWER(?)", (caminho_destino, codigo))
    conn.commit()
    conn.close()
    
    return {"mensagem": "Nota Fiscal salva com sucesso!", "arquivo": nome_arquivo}


@app.get("/api/alertas")
def get_alertas_estoque():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT 
            i.codigo, i.nome, i.estoque_minimo,
            COALESCE(SUM(CASE WHEN m.tipo = 'Entrada' THEN m.quantidade ELSE 0 END), 0) - 
            COALESCE(SUM(CASE WHEN m.tipo = 'Saída' THEN m.quantidade ELSE 0 END), 0) AS saldo_atual
        FROM itens i
        LEFT JOIN movimentacoes m ON i.codigo = m.codigo_item
        GROUP BY i.codigo
        HAVING saldo_atual <= i.estoque_minimo
    ''')
    alertas = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return alertas

@app.get("/api/exportar/csv")
def exportar_csv():
    conn = get_db_connection()
    query = '''
        SELECT 
            i.codigo AS Codigo,
            i.nome AS Item,
            i.localizacao AS Localizacao,
            i.responsavel AS Responsavel,
            COALESCE(SUM(CASE WHEN m.tipo = 'Entrada' THEN m.quantidade ELSE 0 END), 0) - 
            COALESCE(SUM(CASE WHEN m.tipo = 'Saída' THEN m.quantidade ELSE 0 END), 0) AS SaldoAtual,
            i.estoque_minimo AS EstoqueMinimo,
            CASE 
                WHEN (
                    COALESCE(SUM(CASE WHEN m.tipo = 'Entrada' THEN m.quantidade ELSE 0 END), 0) - 
                    COALESCE(SUM(CASE WHEN m.tipo = 'Saída' THEN m.quantidade ELSE 0 END), 0)
                ) <= 0 THEN 'Zerado'
                WHEN (
                    COALESCE(SUM(CASE WHEN m.tipo = 'Entrada' THEN m.quantidade ELSE 0 END), 0) - 
                    COALESCE(SUM(CASE WHEN m.tipo = 'Saída' THEN m.quantidade ELSE 0 END), 0)
                ) < i.estoque_minimo THEN 'Repor'
                ELSE 'OK'
            END AS Status
        FROM itens i
        LEFT JOIN movimentacoes m ON i.codigo = m.codigo_item
        GROUP BY i.codigo, i.nome, i.estoque_minimo, i.localizacao, i.responsavel;
    '''
    cursor = conn.cursor()
    cursor.execute(query)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    
    writer.writerow(["Código", "Item", "Localização", "Responsável", "Saldo Atual", "Estoque Mínimo", "Status"])
    
    for row in rows:
        writer.writerow([
            row["Codigo"], 
            row["Item"], 
            row["Localizacao"] or "Geral", 
            row["Responsavel"] or "Nenhum", 
            row["SaldoAtual"], 
            row["EstoqueMinimo"], 
            row["Status"]
        ])
    
    csv_content = output.getvalue()
    
    return Response(
        content=csv_content.encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=relatorio_estoque_statiot.csv"}
    )

@app.put("/api/itens/{codigo}")
def atualizar_item(codigo: str, item: ItemUpdate):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT codigo FROM itens WHERE LOWER(codigo) = LOWER(?)", (codigo,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    
    cursor.execute(
        "UPDATE itens SET nome = ?, estoque_minimo = ?, localizacao = ?, responsavel = ? WHERE LOWER(codigo) = LOWER(?)",
        (item.nome, item.estoque_minimo, item.localizacao, item.responsavel, codigo)
    )
    conn.commit()
    conn.close()
    return {"mensagem": "Item atualizado com sucesso!"}

@app.delete("/api/itens/{codigo}")
def excluir_item(codigo: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT codigo FROM itens WHERE LOWER(codigo) = LOWER(?)", (codigo,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    
    cursor.execute("DELETE FROM movimentacoes WHERE LOWER(codigo_item) = LOWER(?)", (codigo,))
    cursor.execute("DELETE FROM itens WHERE LOWER(codigo) = LOWER(?)", (codigo,))
    conn.commit()
    conn.close()
    return {"mensagem": "Item excluído com sucesso!"}

@app.get("/api/usuarios")
def listar_usuarios():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, email, role FROM usuarios")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "nome": r[1], "email": r[2], "role": r[3]} for r in rows]

@app.post("/api/usuarios")
def criar_usuario(user: UsuarioCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO usuarios (nome, email, senha, role) VALUES (?, ?, ?, ?)",
            (user.nome, user.email, user.senha, user.role)
        )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail="E-mail já cadastrado ou dados inválidos.")
    conn.close()
    return {"mensagem": "Usuário cadastrado com sucesso!"}

@app.delete("/api/usuarios/{user_id}")
def excluir_usuario(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM usuarios WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"mensagem": "Usuário removido com sucesso!"}

@app.post("/api/login")
def fazer_login(payload: LoginPayload):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT nome, role FROM usuarios WHERE LOWER(email) = LOWER(?) AND senha = ?", 
                   (payload.email.strip(), payload.senha))
    user = cursor.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    
    return {"nome": user[0], "role": user[1]}


@app.get("/api/admin/auditoria")
def get_relatorio_auditoria():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Rastreabilidade Completa (Últimas 100 movimentações)
    cursor.execute('''
        SELECT m.id, m.data, m.codigo_item, i.nome as nome_item, m.tipo, m.quantidade, m.usuario
        FROM movimentacoes m
        LEFT JOIN itens i ON m.codigo_item = i.codigo
        ORDER BY m.id DESC LIMIT 100
    ''')
    movimentacoes = [dict(row) for row in cursor.fetchall()]
    
    # 2. Ranking de Usuários (Quem fez mais movimentações)
    cursor.execute('''
        SELECT usuario, COUNT(id) as total_movimentacoes 
        FROM movimentacoes 
        GROUP BY usuario 
        ORDER BY total_movimentacoes DESC
    ''')
    ranking = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return {"movimentacoes": movimentacoes, "ranking": ranking}