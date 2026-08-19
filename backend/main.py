from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
from datetime import date

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
            estoque_minimo INTEGER NOT NULL
        )
    ''')

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

    # Dados iniciais se estiver vazio
    cursor.execute("INSERT OR IGNORE INTO itens (codigo, nome, estoque_minimo) VALUES ('M8x30', 'Parafuso sextavado M8x30', 200)")
    cursor.execute("INSERT OR IGNORE INTO itens (codigo, nome, estoque_minimo) VALUES ('CH3', 'Chapa aço carbono 3mm', 10)")

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

class MovimentacaoCreate(BaseModel):
    codigo_item: str
    tipo: str  # 'Entrada' ou 'Saída'
    quantidade: int = Field(..., gt=0, description="A quantidade deve ser maior que zero")

# 1. Rota de Listagem do Estoque (Calcula o Saldo via SQL)
@app.get("/api/estoque")
def get_estoque():
    conn = get_db_connection()
    query = '''
        SELECT 
            i.nome AS Item,
            i.codigo AS Codigo,
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
        GROUP BY i.codigo, i.nome, i.estoque_minimo;
    '''
    cursor = conn.cursor()
    cursor.execute(query)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

# 2. Rota para Cadastrar Novo Item
@app.post("/api/itens")
def criar_item(item: ItemCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO itens (codigo, nome, estoque_minimo) VALUES (?, ?, ?)",
            (item.codigo, item.nome, item.estoque_minimo)
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
    
    # Verifica se o item existe
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