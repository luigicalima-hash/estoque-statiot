from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import sqlite3
from datetime import date
import io
import qrcode
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import os
from PIL import Image

app = FastAPI(title="Statiot Inventory API")

# Habilita o CORS para permitir requisições do seu frontend (GitHub Pages)
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
            localizacao TEXT DEFAULT 'Geral'
        )
    ''')

    # Migration de segurança caso a tabela já exista sem a coluna localizacao
    try:
        cursor.execute("ALTER TABLE itens ADD COLUMN localizacao TEXT DEFAULT 'Geral'")
    except sqlite3.OperationalError:
        pass # A coluna já existe

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

    # Dados iniciais se estiver vazio (agora com localização)
    cursor.execute("INSERT OR IGNORE INTO itens (codigo, nome, estoque_minimo, localizacao) VALUES ('M8x30', 'Parafuso sextavado M8x30', 200, 'Prateleira A - Setor 02')")
    cursor.execute("INSERT OR IGNORE INTO itens (codigo, nome, estoque_minimo, localizacao) VALUES ('CH3', 'Chapa aço carbono 3mm', 10, 'Almoxarifado Central - Prap. C')")

    cursor.execute("SELECT COUNT(*) FROM movimentacoes")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO movimentacoes (data, codigo_item, tipo, quantidade) VALUES ('2026-08-17', 'M8x30', 'Entrada', 500)")
        cursor.execute("INSERT INTO movimentacoes (data, codigo_item, tipo, quantidade) VALUES ('2026-08-18', 'M8x30', 'Saída', 120)")
        cursor.execute("INSERT INTO movimentacoes (data, codigo_item, tipo, quantidade) VALUES ('2026-08-18', 'CH3', 'Entrada', 20)")
    
    conn.commit()
    conn.close()

init_db()

# Modelos de Dados (Validação com Pydantic)
class ItemCreate(BaseModel):
    codigo: str
    nome: str
    estoque_minimo: int
    localizacao: str = Field(..., description="Localização física ou setor do ativo")

class MovimentacaoCreate(BaseModel):
    codigo_item: str
    tipo: str  # 'Entrada' ou 'Saída'
    quantidade: int = Field(..., gt=0, description="A quantidade deve ser maior que zero")

# 1. Rota de Listagem do Estoque (Calcula o Saldo via SQL e retorna a Localização)
@app.get("/api/estoque")
def get_estoque():
    conn = get_db_connection()
    query = '''
        SELECT 
            i.nome AS Item,
            i.codigo AS Codigo,
            i.localizacao AS Localizacao,
            COALESCE(SUM(CASE WHEN m.tipo = 'Entrada' THEN m.quantidade ELSE 0 END), 0) - 
            COALESCE(SUM(CASE WHEN m.tipo = 'Saída' THEN m.quantidade ELSE 0 END), 0) AS Saldo,
            i.estoque_minimo AS EstoqueMinimo,
            CASE 
                WHEN (
                    COALESCE(SUM(CASE WHEN m.tipo = 'Entrada' THEN m.quantidade ELSE 0 END), 0) - 
                    COALESCE(SUM(CASE WHEN m.tipo = 'Saída' THEN m.quantidade ELSE 0 END), 0)
                ) < i.estoque_minimo THEN 'Repor'
                ELSE 'OK'
            END AS Status
        FROM itens i
        LEFT JOIN movimentacoes m ON i.codigo = m.codigo_item
        GROUP BY i.codigo, i.nome, i.estoque_minimo, i.localizacao;
    '''
    cursor = conn.cursor()
    cursor.execute(query)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

# 2. Rota para Cadastrar Novo Item (Com Localização)
@app.post("/api/itens")
def criar_item(item: ItemCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO itens (codigo, nome, estoque_minimo, localizacao) VALUES (?, ?, ?, ?)",
            (item.codigo, item.nome, item.estoque_minimo, item.localizacao)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Este código de item já existe.")
    conn.close()
    return {"mensagem": "Item cadastrado com sucesso!", "codigo": item.codigo}

# 3. Rota para Registrar Movimentação (Entrada ou Saída)
@app.post("/api/movimentacoes")
def criar_movimentacao(mov: MovimentacaoCreate):
    if mov.tipo not in ["Entrada", "Saída"]:
        raise HTTPException(status_code=400, detail="Tipo deve ser 'Entrada' ou 'Saída'.")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT codigo FROM itens WHERE codigo = ?", (mov.codigo_item,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Item não encontrado no cadastro.")
    
    data_atual = str(date.today())
    cursor.execute(
        "INSERT INTO movimentacoes (data, codigo_item, tipo, quantidade) VALUES (?, ?, ?, ?)",
        (data_atual, mov.codigo_item, mov.tipo, mov.quantidade)
    )
    conn.commit()
    conn.close()
    return {"mensagem": "Movimentação registrada com sucesso!"}

# 4. Rota para Gerar e Baixar Etiqueta em PDF com QR Code
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
    
    # Borda externa
    c.setLineWidth(0.5)
    c.rect(2 * mm, 2 * mm, largura_etiqueta - 4 * mm, altura_etiqueta - 4 * mm)
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(base_dir, "..", "img", "log-png.png")
    
    if os.path.exists(logo_path):
        try:
            c.drawImage(logo_path, 5 * mm, 38 * mm, width=14 * mm, height=7 * mm, preserveAspectRatio=True, mask='auto')
            c.setFont("Helvetica-Bold", 8.5)
            c.drawString(21 * mm, 41 * mm, "STATIOT — CONTROLE DE ATIVOS")
        except Exception as e:
            print(f"Erro ao renderizar logo: {e}")
            c.setFont("Helvetica-Bold", 10)
            c.drawString(5 * mm, 42 * mm, "STATIOT — CONTROLE DE ATIVOS")
    else:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(5 * mm, 42 * mm, "STATIOT — CONTROLE DE ATIVOS")
        
    c.line(5 * mm, 36 * mm, largura_etiqueta - 5 * mm, 36 * mm)
    
    # QR Code no lado direito
    c.drawImage(ImageReader(qr_buffer), 47 * mm, 8 * mm, width=28 * mm, height=28 * mm)
    
    # Textos do ativo
    c.setFont("Helvetica-Bold", 8)
    c.drawString(6 * mm, 30 * mm, "CÓDIGO / ATIVO:")
    c.setFont("Helvetica", 10)
    c.drawString(6 * mm, 25 * mm, codigo)
    
    c.setFont("Helvetica-Bold", 8)
    c.drawString(6 * mm, 19 * mm, "DESCRIÇÃO:")
    c.setFont("Helvetica", 9)
    c.drawString(6 * mm, 14 * mm, nome_item[:22])
    
    # Rodapé
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