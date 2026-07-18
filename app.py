import os
import re
import unicodedata
import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, send_from_directory, render_template, make_response, session, redirect, url_for
from dotenv import load_dotenv
from flask_cors import CORS
import datetime
import traceback
import decimal
import bcrypt

load_dotenv()

app = Flask(__name__, static_folder='.', static_url_path='', template_folder='templates')
app.secret_key = os.getenv('SECRET_KEY', 'guiarodizio-secret-key-2025')
CORS(app)

# ── Conexão ──────────────────────────────────────────────────
def get_db_connection():
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    return conn

# ── Formatador de dados (datas, decimais) ────────────────────
def format_db_data(data_dict):
    if not isinstance(data_dict, dict):
        return data_dict
    formatted = {}
    for key, value in data_dict.items():
        if isinstance(value, datetime.date):
            formatted[key] = value.strftime('%d/%m/%Y') if value else None
        elif isinstance(value, decimal.Decimal):
            try:
                formatted[key] = float(value)
            except (TypeError, ValueError):
                formatted[key] = None
        else:
            formatted[key] = value
    return formatted

# ── Auth helper ──────────────────────────────────────────────
def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_id'):
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated


# ── Slugs de pratos (para SEO — /pratos/<slug>) ───────────────
def gerar_slug(texto):
    """Transforma 'Alcatra de Boi!' em 'alcatra-de-boi'."""
    if not texto:
        return ''
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    texto = texto.lower().strip()
    texto = re.sub(r'[^a-z0-9]+', '-', texto)
    texto = re.sub(r'-+', '-', texto).strip('-')
    return texto

def _slug_prato_disponivel(cur, slug, excluir_id=None):
    if excluir_id:
        cur.execute("SELECT id FROM pratos WHERE slug = %s AND id != %s", (slug, excluir_id))
    else:
        cur.execute("SELECT id FROM pratos WHERE slug = %s", (slug,))
    return cur.fetchone() is None

def gerar_slug_unico_prato(cur, nome, excluir_id=None):
    """Gera um slug a partir do nome, evitando colisão com outros pratos."""
    base = gerar_slug(nome) or 'prato'
    slug = base
    i = 2
    while not _slug_prato_disponivel(cur, slug, excluir_id):
        slug = f"{base}-{i}"
        i += 1
    return slug

def ensure_pratos_slug_column():
    """Cria a coluna 'slug' em pratos (se não existir) e preenche os pratos
    que ainda não têm slug — roda sozinho ao subir o app, sem precisar de
    migração manual no banco."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("ALTER TABLE pratos ADD COLUMN IF NOT EXISTS slug VARCHAR(255)")
        conn.commit()

        cur.execute("SELECT id, nome FROM pratos WHERE slug IS NULL OR slug = '' ORDER BY id")
        pendentes = cur.fetchall()
        for row in pendentes:
            novo_slug = gerar_slug_unico_prato(cur, row['nome'], excluir_id=row['id'])
            cur.execute("UPDATE pratos SET slug = %s WHERE id = %s", (novo_slug, row['id']))
            conn.commit()

        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pratos_slug ON pratos (slug)")
        conn.commit()
        cur.close()
        if pendentes:
            print(f"✅ Slugs de pratos: {len(pendentes)} gerado(s) automaticamente.")
    except Exception as e:
        print("⚠️  Não foi possível preparar a coluna 'slug' em pratos:", e)
        traceback.print_exc()
    finally:
        if conn: conn.close()

ensure_pratos_slug_column()


# ── Segmentação de banners (categoria / cidade / bairro) ──────
def ensure_banners_target_columns():
    """Cria as colunas de segmentação em 'banners' se ainda não existirem —
    roda sozinho ao subir o app, sem precisar de migração manual."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("ALTER TABLE banners ADD COLUMN IF NOT EXISTS categoria_id INTEGER")
        cur.execute("ALTER TABLE banners ADD COLUMN IF NOT EXISTS cidade VARCHAR(120)")
        cur.execute("ALTER TABLE banners ADD COLUMN IF NOT EXISTS bairro VARCHAR(120)")
        cur.execute("ALTER TABLE banners ADD COLUMN IF NOT EXISTS prato_id INTEGER")
        conn.commit()
        cur.close()
    except Exception as e:
        print("⚠️  Não foi possível preparar colunas de segmentação de banners:", e)
        traceback.print_exc()
    finally:
        if conn: conn.close()

ensure_banners_target_columns()


# ════════════════════════════════════════════════════════════
#  ROTAS DE PÁGINAS HTML (templates)
# ════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/japones')
def japones():
    return render_template('categoria.html', categoria_slug='japones')

@app.route('/carnes')
def carnes():
    return render_template('categoria.html', categoria_slug='carnes')

@app.route('/pizza')
def pizza():
    return render_template('categoria.html', categoria_slug='pizza')

@app.route('/mexicano')
def mexicano():
    return render_template('categoria.html', categoria_slug='mexicano')

@app.route('/blog')
def blog():
    return render_template('blog.html')

@app.route('/blog/<slug>')
def blog_post(slug):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM posts WHERE slug = %s AND ativo = TRUE", (slug,))
        post = cur.fetchone()
        cur.close()
        if not post:
            return "Post não encontrado", 404
        return render_template('post-detalhe.html', post=format_db_data(dict(post)))
    except Exception as e:
        traceback.print_exc()
        return "Erro ao carregar post", 500
    finally:
        if conn: conn.close()

@app.route('/restaurantes/<slug>')
def restaurante_detalhe(slug):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT r.*, c.nome as categoria_nome, c.slug as categoria_slug, p.nome as plano_nome
            FROM restaurantes r
            LEFT JOIN categorias c ON r.categoria_id = c.id
            LEFT JOIN planos p ON r.plano_id = p.id
            WHERE r.slug = %s AND r.ativo = TRUE
        """, (slug,))
        restaurante = cur.fetchone()
        if not restaurante:
            return "Restaurante não encontrado", 404
        # Busca pratos que esse restaurante serve
        cur.execute("""
            SELECT pr.* FROM pratos pr
            JOIN restaurante_pratos rp ON pr.id = rp.prato_id
            WHERE rp.restaurante_id = %s
        """, (restaurante['id'],))
        pratos = [format_db_data(dict(p)) for p in cur.fetchall()]
        cur.close()
        return render_template('restaurante-detalhe.html',
                               restaurante=format_db_data(dict(restaurante)),
                               pratos=pratos)
    except Exception as e:
        traceback.print_exc()
        return "Erro ao carregar restaurante", 500
    finally:
        if conn: conn.close()


@app.route('/pratos/<slug>')
def prato_detalhe(slug):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.*, c.nome as categoria_nome, c.slug as categoria_slug
            FROM pratos p
            LEFT JOIN categorias c ON p.categoria_id = c.id
            WHERE p.slug = %s
        """, (slug,))
        prato = cur.fetchone()
        if not prato:
            return "Prato não encontrado", 404

        # Restaurantes que servem este prato (mesmo critério da API pública)
        cur.execute("""
            SELECT r.id, r.nome, r.slug, r.foto_url, r.bairro, r.cidade,
                   r.lat, r.lng, p2.destaque, p2.aparece_em_pratos
            FROM restaurantes r
            JOIN restaurante_pratos rp ON r.id = rp.restaurante_id
            JOIN planos p2 ON r.plano_id = p2.id
            WHERE rp.prato_id = %s AND r.ativo = TRUE AND p2.aparece_em_pratos = TRUE
            ORDER BY p2.destaque DESC, r.nome
        """, (prato['id'],))
        restaurantes = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()

        return render_template('prato-detalhe.html',
                               prato=format_db_data(dict(prato)),
                               restaurantes=restaurantes)
    except Exception as e:
        traceback.print_exc()
        return "Erro ao carregar prato", 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  PÁGINA — VAGAS / EMPREGOS
# ════════════════════════════════════════════════════════════

@app.route('/vagas')
def vagas():
    return render_template('vagas.html')


# ════════════════════════════════════════════════════════════
#  API — CATEGORIAS
# ════════════════════════════════════════════════════════════

@app.route('/api/categorias')
def api_categorias():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM categorias WHERE ativo = TRUE ORDER BY nome")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar categorias'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — PRATOS
# ════════════════════════════════════════════════════════════

@app.route('/api/pratos')
def api_pratos():
    conn = None
    try:
        categoria_slug = request.args.get('categoria')
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if categoria_slug:
            cur.execute("""
                SELECT p.* FROM pratos p
                JOIN categorias c ON p.categoria_id = c.id
                WHERE c.slug = %s
                ORDER BY p.destaque DESC, p.nome
            """, (categoria_slug,))
        else:
            cur.execute("SELECT * FROM pratos ORDER BY destaque DESC, nome")

        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar pratos'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — AVALIAÇÃO DE PRATOS (ESTRELAS) — NOVO
# ════════════════════════════════════════════════════════════

@app.route('/api/pratos/<int:prato_id>/nota', methods=['GET'])
def api_prato_nota(prato_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT ROUND(AVG(nota)::numeric, 1) as media, COUNT(*) as total
            FROM avaliacoes_pratos
            WHERE prato_id = %s
        """, (prato_id,))
        row = cur.fetchone()
        cur.close()
        return jsonify({
            'media': float(row['media']) if row['media'] else 0,
            'total': int(row['total'])
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar nota'}), 500
    finally:
        if conn: conn.close()


@app.route('/api/pratos/<int:prato_id>/avaliar', methods=['POST'])
def api_prato_avaliar(prato_id):
    conn = None
    try:
        data  = request.get_json()
        nota  = data.get('nota')
        if not nota or int(nota) < 1 or int(nota) > 5:
            return jsonify({'ok': False, 'error': 'Nota inválida (1 a 5)'}), 400

        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip and ',' in ip:
            ip = ip.split(',')[0].strip()

        conn = get_db_connection()
        cur  = conn.cursor()
        # INSERT OR UPDATE — se já votou, atualiza a nota
        cur.execute("""
            INSERT INTO avaliacoes_pratos (prato_id, ip, nota)
            VALUES (%s, %s, %s)
            ON CONFLICT (prato_id, ip) DO UPDATE SET nota = EXCLUDED.nota
        """, (prato_id, ip, int(nota)))

        # Atualiza cache de média e total na tabela pratos
        cur.execute("""
            UPDATE pratos SET
                media_nota  = (SELECT ROUND(AVG(nota)::numeric, 2) FROM avaliacoes_pratos WHERE prato_id = %s),
                total_votos = (SELECT COUNT(*) FROM avaliacoes_pratos WHERE prato_id = %s)
            WHERE id = %s
        """, (prato_id, prato_id, prato_id))

        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': 'Erro ao salvar avaliação'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — RESTAURANTES
# ════════════════════════════════════════════════════════════

@app.route('/api/restaurantes')
def api_restaurantes():
    conn = None
    try:
        categoria_slug = request.args.get('categoria')
        lat    = request.args.get('lat', type=float)
        lng    = request.args.get('lng', type=float)
        cidade = request.args.get('cidade')   # NOVO
        regiao = request.args.get('regiao')   # NOVO

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Monta filtros extras de cidade e região
        filtros_extra      = ""
        params_extra_before = []
        params_extra_after  = []
        if cidade:
            filtros_extra       += " AND r.cidade = %s"
            params_extra_after.append(cidade)
        if regiao:
            filtros_extra       += " AND r.regiao = %s"
            params_extra_after.append(regiao)

        # Se tiver lat/lng, ordena por proximidade (fórmula Haversine aproximada)
        if lat and lng and categoria_slug:
            cur.execute(f"""
                SELECT r.*, c.nome as categoria_nome, c.slug as categoria_slug,
                    (6371 * acos(cos(radians(%s)) * cos(radians(r.lat)) *
                    cos(radians(r.lng) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(r.lat)))) AS distancia_km
                FROM restaurantes r
                JOIN categorias c ON r.categoria_id = c.id
                WHERE c.slug = %s AND r.ativo = TRUE AND r.lat IS NOT NULL
                {filtros_extra}
                ORDER BY distancia_km
            """, [lat, lng, lat, categoria_slug] + params_extra_after)
        elif lat and lng:
            cur.execute(f"""
                SELECT r.*, c.nome as categoria_nome, c.slug as categoria_slug,
                    (6371 * acos(cos(radians(%s)) * cos(radians(r.lat)) *
                    cos(radians(r.lng) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(r.lat)))) AS distancia_km
                FROM restaurantes r
                JOIN categorias c ON r.categoria_id = c.id
                WHERE r.ativo = TRUE AND r.lat IS NOT NULL
                {filtros_extra}
                ORDER BY distancia_km
            """, [lat, lng, lat] + params_extra_after)
        elif categoria_slug:
            cur.execute(f"""
                SELECT r.*, c.nome as categoria_nome, c.slug as categoria_slug
                FROM restaurantes r
                JOIN categorias c ON r.categoria_id = c.id
                WHERE c.slug = %s AND r.ativo = TRUE
                {filtros_extra}
                ORDER BY r.nome
            """, [categoria_slug] + params_extra_after)
        else:
            cur.execute(f"""
                SELECT r.*, c.nome as categoria_nome, c.slug as categoria_slug
                FROM restaurantes r
                JOIN categorias c ON r.categoria_id = c.id
                WHERE r.ativo = TRUE
                {filtros_extra}
                ORDER BY r.nome
            """, params_extra_after)

        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar restaurantes'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — CIDADES E REGIÕES DISPONÍVEIS — NOVO
# ════════════════════════════════════════════════════════════

@app.route('/api/cidades')
def api_cidades():
    """Retorna apenas as cidades que têm restaurantes ativos cadastrados."""
    conn = None
    try:
        categoria_slug = request.args.get('categoria')
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if categoria_slug:
            cur.execute("""
                SELECT DISTINCT r.cidade
                FROM restaurantes r
                JOIN categorias c ON r.categoria_id = c.id
                WHERE r.ativo = TRUE AND r.cidade IS NOT NULL AND c.slug = %s
                ORDER BY r.cidade
            """, (categoria_slug,))
        else:
            cur.execute("""
                SELECT DISTINCT cidade FROM restaurantes
                WHERE ativo = TRUE AND cidade IS NOT NULL
                ORDER BY cidade
            """)
        cidades = [row['cidade'] for row in cur.fetchall()]
        cur.close()
        return jsonify(cidades)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar cidades'}), 500
    finally:
        if conn: conn.close()


@app.route('/api/regioes')
def api_regioes():
    """Retorna as regiões disponíveis, opcionalmente filtradas por cidade e/ou categoria."""
    conn = None
    try:
        cidade         = request.args.get('cidade')
        categoria_slug = request.args.get('categoria')
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        params = []
        filtros = "WHERE r.ativo = TRUE AND r.regiao IS NOT NULL"
        if cidade:
            filtros += " AND r.cidade = %s"
            params.append(cidade)
        if categoria_slug:
            filtros += " AND c.slug = %s"
            params.append(categoria_slug)

        cur.execute(f"""
            SELECT DISTINCT r.regiao
            FROM restaurantes r
            LEFT JOIN categorias c ON r.categoria_id = c.id
            {filtros}
            ORDER BY r.regiao
        """, params)
        regioes = [row['regiao'] for row in cur.fetchall()]
        cur.close()
        return jsonify(regioes)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar regiões'}), 500
    finally:
        if conn: conn.close()


# API: restaurantes que servem um prato específico (para "disponível em...")
@app.route('/api/pratos/<int:prato_id>/restaurantes')
def api_restaurantes_por_prato(prato_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT r.id, r.nome, r.slug, r.foto_url, r.bairro, r.cidade,
                   r.lat, r.lng, p.destaque, p.aparece_em_pratos
            FROM restaurantes r
            JOIN restaurante_pratos rp ON r.id = rp.restaurante_id
            JOIN planos p ON r.plano_id = p.id
            WHERE rp.prato_id = %s AND r.ativo = TRUE AND p.aparece_em_pratos = TRUE
            ORDER BY p.destaque DESC, r.nome
        """, (prato_id,))
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — COMENTÁRIOS DE RESTAURANTES — NOVO
# ════════════════════════════════════════════════════════════

@app.route('/api/restaurantes/<int:restaurante_id>/comentarios', methods=['GET'])
def api_comentarios(restaurante_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, nome, texto, criado_em
            FROM comentarios
            WHERE restaurante_id = %s AND aprovado = TRUE
            ORDER BY criado_em DESC
        """, (restaurante_id,))
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar comentários'}), 500
    finally:
        if conn: conn.close()


@app.route('/api/restaurantes/<int:restaurante_id>/comentarios', methods=['POST'])
def api_comentario_novo(restaurante_id):
    conn = None
    try:
        data  = request.get_json()
        nome  = (data.get('nome') or '').strip()
        texto = (data.get('texto') or '').strip()
        email = (data.get('email') or '').strip()
        if not nome or not texto:
            return jsonify({'ok': False, 'error': 'Nome e comentário são obrigatórios'}), 400
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO comentarios (restaurante_id, nome, email, texto, aprovado)
            VALUES (%s, %s, %s, %s, FALSE)
        """, (restaurante_id, nome, email, texto))
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'msg': 'Comentário enviado e aguardando aprovação!'})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': 'Erro ao salvar comentário'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — BLOG
# ════════════════════════════════════════════════════════════

@app.route('/api/blog')
def api_blog():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM posts WHERE ativo = TRUE ORDER BY criado_em DESC")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar posts'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — VAGAS (pública)
# ════════════════════════════════════════════════════════════

@app.route('/api/vagas')
def api_vagas():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT v.*, r.nome as restaurante_nome, r.slug as restaurante_slug
            FROM vagas v
            LEFT JOIN restaurantes r ON v.restaurante_id = r.id
            WHERE v.ativo = TRUE
            ORDER BY v.destaque DESC, v.criado_em DESC
        """)
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar vagas'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — CANDIDATOS (pública: GET lista, POST cadastro)
# ════════════════════════════════════════════════════════════

@app.route('/api/candidatos', methods=['GET', 'POST'])
def api_candidatos():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("""
                SELECT id, nome, cargo, cidade, bairro, telefone,
                       descricao, experiencia, criado_em
                FROM candidatos
                WHERE ativo = TRUE
                ORDER BY criado_em DESC
            """)
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        # POST — cadastro pelo profissional
        data = request.get_json()
        nome       = (data.get('nome') or '').strip()
        cargo      = (data.get('cargo') or '').strip()
        telefone   = (data.get('telefone') or '').strip()

        if not nome or not cargo or not telefone:
            return jsonify({'ok': False, 'error': 'Nome, cargo e telefone são obrigatórios'}), 400

        cur.execute("""
            INSERT INTO candidatos (nome, cargo, cidade, bairro, telefone, descricao, experiencia, ativo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
        """, (
            nome, cargo,
            (data.get('cidade') or 'São Paulo').strip(),
            (data.get('bairro') or '').strip(),
            telefone,
            (data.get('descricao') or '').strip(),
            (data.get('experiencia') or '').strip(),
        ))
        cur.close()
        return jsonify({'ok': True})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': 'Erro interno'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  ADMIN — LOGIN / LOGOUT
# ════════════════════════════════════════════════════════════

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        data = request.get_json()
        email = data.get('email', '').strip()
        senha = data.get('senha', '')
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM usuarios WHERE email = %s", (email,))
            user = cur.fetchone()
            cur.close()
            if user and bcrypt.checkpw(senha.encode('utf-8'), user['senha_hash'].encode('utf-8')):
                session['admin_id'] = user['id']
                session['admin_nome'] = user['nome']
                return jsonify({'ok': True})
            return jsonify({'ok': False, 'error': 'E-mail ou senha incorretos'}), 401
        except Exception as e:
            traceback.print_exc()
            return jsonify({'error': 'Erro interno'}), 500
        finally:
            if conn: conn.close()
    return render_template('admin/login.html')

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect('/admin/login')

@app.route('/admin')
@login_required
def admin_index():
    return render_template('admin/index.html', nome=session.get('admin_nome'))


# ════════════════════════════════════════════════════════════
#  API ADMIN — VAGAS (requer login)
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/vagas', methods=['GET', 'POST'])
@login_required
def api_admin_vagas():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'GET':
            cur.execute("""
                SELECT v.*, r.nome as restaurante_nome
                FROM vagas v
                LEFT JOIN restaurantes r ON v.restaurante_id = r.id
                ORDER BY v.criado_em DESC
            """)
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)

        data = request.get_json()
        cur.execute("""
            INSERT INTO vagas (restaurante_id, cargo, descricao, regime, salario,
                               cidade, bairro, contato, whatsapp, ativo, destaque)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            data.get('restaurante_id') or None,
            data.get('cargo', '').strip(),
            data.get('descricao', '').strip(),
            data.get('regime', '').strip(),
            data.get('salario', '').strip(),
            data.get('cidade', 'São Paulo').strip(),
            data.get('bairro', '').strip(),
            data.get('contato', '').strip(),
            data.get('whatsapp', '').strip(),
            data.get('ativo', True),
            data.get('destaque', False),
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/vagas/<int:vaga_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_vaga(vaga_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if request.method == 'DELETE':
            cur.execute("DELETE FROM vagas WHERE id = %s", (vaga_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})

        data = request.get_json()
        cur.execute("""
            UPDATE vagas SET
                restaurante_id = %s, cargo = %s, descricao = %s,
                regime = %s, salario = %s, cidade = %s, bairro = %s,
                contato = %s, whatsapp = %s, ativo = %s, destaque = %s
            WHERE id = %s
        """, (
            data.get('restaurante_id') or None,
            data.get('cargo', '').strip(),
            data.get('descricao', '').strip(),
            data.get('regime', '').strip(),
            data.get('salario', '').strip(),
            data.get('cidade', 'São Paulo').strip(),
            data.get('bairro', '').strip(),
            data.get('contato', '').strip(),
            data.get('whatsapp', '').strip(),
            data.get('ativo', True),
            data.get('destaque', False),
            vaga_id,
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — CANDIDATOS (requer login)
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/candidatos')
@login_required
def api_admin_candidatos():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM candidatos ORDER BY criado_em DESC")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/candidatos/<int:cand_id>', methods=['DELETE'])
@login_required
def api_admin_candidato(cand_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM candidatos WHERE id = %s", (cand_id,))
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/candidatos/<int:cand_id>/aprovar', methods=['POST'])
@login_required
def api_admin_aprovar_candidato(cand_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE candidatos SET ativo = TRUE WHERE id = %s", (cand_id,))
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — COMENTÁRIOS (requer login) — NOVO
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/comentarios', methods=['GET'])
@login_required
def api_admin_comentarios():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.*, r.nome as restaurante_nome
            FROM comentarios c
            LEFT JOIN restaurantes r ON c.restaurante_id = r.id
            ORDER BY c.aprovado ASC, c.criado_em DESC
        """)
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/comentarios/<int:com_id>/aprovar', methods=['POST'])
@login_required
def api_admin_aprovar_comentario(com_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("UPDATE comentarios SET aprovado = TRUE WHERE id = %s", (com_id,))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/comentarios/<int:com_id>', methods=['DELETE'])
@login_required
def api_admin_deletar_comentario(com_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM comentarios WHERE id = %s", (com_id,))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  SITEMAP
# ════════════════════════════════════════════════════════════

@app.route('/sitemap.xml')
def sitemap():
    conn = None
    urls = [
        'https://www.guiadorodizio.com.br/',
        'https://www.guiadorodizio.com.br/japones',
        'https://www.guiadorodizio.com.br/carnes',
        'https://www.guiadorodizio.com.br/pizza',
        'https://www.guiadorodizio.com.br/mexicano',
        'https://www.guiadorodizio.com.br/blog',
        'https://www.guiadorodizio.com.br/vagas',
    ]
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT slug FROM restaurantes WHERE ativo = TRUE AND slug IS NOT NULL")
        for row in cur.fetchall():
            urls.append(f'https://www.guiadorodizio.com.br/restaurantes/{row[0]}')
        cur.execute("SELECT slug FROM pratos WHERE slug IS NOT NULL")
        for row in cur.fetchall():
            urls.append(f'https://www.guiadorodizio.com.br/pratos/{row[0]}')
        cur.execute("SELECT slug FROM posts WHERE ativo = TRUE AND slug IS NOT NULL")
        for row in cur.fetchall():
            urls.append(f'https://www.guiadorodizio.com.br/blog/{row[0]}')
        cur.close()
    except Exception as e:
        print(f"AVISO: Erro ao buscar URLs para sitemap: {e}")
    finally:
        if conn: conn.close()

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url in urls:
        xml += f'  <url><loc>{url}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>\n'
    xml += '</urlset>'
    return make_response(xml, 200, {'Content-Type': 'application/xml'})


# ════════════════════════════════════════════════════════════
#  API ADMIN — CATEGORIAS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/categorias', methods=['GET', 'POST'])
@login_required
def api_admin_categorias():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if request.method == 'GET':
            cur.execute("SELECT * FROM categorias ORDER BY nome")
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)
        data = request.get_json()
        cur.execute("""
            INSERT INTO categorias (nome, slug, icone_url, ativo)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (data['nome'], data['slug'], data.get('icone_url', ''), data.get('ativo', True)))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()

@app.route('/api/admin/categorias/<int:cat_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_categoria(cat_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if request.method == 'DELETE':
            cur.execute("DELETE FROM categorias WHERE id = %s", (cat_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})
        data = request.get_json()
        cur.execute("""
            UPDATE categorias SET nome=%s, slug=%s, icone_url=%s, ativo=%s WHERE id=%s
        """, (data['nome'], data['slug'], data.get('icone_url', ''), data.get('ativo', True), cat_id))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — PRATOS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/pratos', methods=['GET', 'POST'])
@login_required
def api_admin_pratos():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if request.method == 'GET':
            cur.execute("SELECT * FROM pratos ORDER BY nome")
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)
        data = request.get_json()
        nome = data.get('nome', '')
        slug_informado = gerar_slug(data.get('slug', '')) if data.get('slug') else ''
        slug = slug_informado or gerar_slug_unico_prato(cur, nome)
        # Garante que o slug (mesmo se digitado manualmente) não colide com outro prato
        if not _slug_prato_disponivel(cur, slug):
            slug = gerar_slug_unico_prato(cur, slug)
        cur.execute("""
            INSERT INTO pratos (nome, categoria_id, foto_url, ingredientes, descricao, cta_texto, cta_url, destaque, slug)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (
            nome, data.get('categoria_id') or None,
            data.get('foto_url', ''), data.get('ingredientes', ''),
            data.get('descricao', ''), data.get('cta_texto', ''),
            data.get('cta_url', ''), data.get('destaque', False), slug
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id, 'slug': slug})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()

@app.route('/api/admin/pratos/<int:prato_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_prato(prato_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if request.method == 'DELETE':
            cur.execute("DELETE FROM pratos WHERE id = %s", (prato_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})
        data = request.get_json()
        nome = data.get('nome', '')
        slug_informado = gerar_slug(data.get('slug', '')) if data.get('slug') else ''
        slug = slug_informado or gerar_slug_unico_prato(cur, nome, excluir_id=prato_id)
        if not _slug_prato_disponivel(cur, slug, excluir_id=prato_id):
            slug = gerar_slug_unico_prato(cur, slug, excluir_id=prato_id)
        cur.execute("""
            UPDATE pratos SET nome=%s, categoria_id=%s, foto_url=%s, ingredientes=%s,
            descricao=%s, cta_texto=%s, cta_url=%s, destaque=%s, slug=%s WHERE id=%s
        """, (
            nome, data.get('categoria_id') or None,
            data.get('foto_url', ''), data.get('ingredientes', ''),
            data.get('descricao', ''), data.get('cta_texto', ''),
            data.get('cta_url', ''), data.get('destaque', False), slug, prato_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'slug': slug})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — RESTAURANTES
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/restaurantes', methods=['GET', 'POST'])
@login_required
def api_admin_restaurantes():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if request.method == 'GET':
            cur.execute("""
                SELECT r.*, c.nome as categoria_nome, p.nome as plano_nome
                FROM restaurantes r
                LEFT JOIN categorias c ON r.categoria_id = c.id
                LEFT JOIN planos p ON r.plano_id = p.id
                ORDER BY r.nome
            """)
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)
        data = request.get_json()
        cur.execute("""
            INSERT INTO restaurantes (nome, slug, categoria_id, plano_id, descricao, endereco,
                bairro, cidade, regiao, telefone, whatsapp, site_url, foto_url, ativo, destaque,
                lat, lng, instagram)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('nome',''), data.get('slug',''),
            data.get('categoria_id') or None, data.get('plano_id') or None,
            data.get('descricao',''), data.get('endereco',''),
            data.get('bairro',''), data.get('cidade','São Paulo'),
            data.get('regiao',''),
            data.get('telefone',''), data.get('whatsapp',''),
            data.get('site_url', '') or data.get('website',''), data.get('foto_url',''),
            data.get('ativo', True), data.get('destaque', False),
            data.get('lat') or None, data.get('lng') or None,
            data.get('instagram','')
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()

@app.route('/api/admin/restaurantes/<int:rest_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_restaurante(rest_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if request.method == 'DELETE':
            cur.execute("DELETE FROM restaurantes WHERE id = %s", (rest_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})
        data = request.get_json()
        cur.execute("""
            UPDATE restaurantes SET nome=%s, slug=%s, categoria_id=%s, plano_id=%s,
            descricao=%s, endereco=%s, bairro=%s, cidade=%s, regiao=%s, telefone=%s, whatsapp=%s,
            site_url=%s, foto_url=%s, ativo=%s, destaque=%s,
            lat=%s, lng=%s, instagram=%s
            WHERE id=%s
        """, (
            data.get('nome',''), data.get('slug',''),
            data.get('categoria_id') or None, data.get('plano_id') or None,
            data.get('descricao',''), data.get('endereco',''),
            data.get('bairro',''), data.get('cidade','São Paulo'),
            data.get('regiao',''),
            data.get('telefone',''), data.get('whatsapp',''),
            data.get('site_url', '') or data.get('website',''), data.get('foto_url',''),
            data.get('ativo', True), data.get('destaque', False),
            data.get('lat') or None, data.get('lng') or None,
            data.get('instagram',''),
            rest_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — BLOG
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/blog', methods=['GET', 'POST'])
@login_required
def api_admin_blog():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if request.method == 'GET':
            cur.execute("SELECT * FROM posts ORDER BY criado_em DESC")
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)
        data = request.get_json()
        cur.execute("""
            INSERT INTO posts (titulo, slug, subtitulo, autor, conteudo, imagem_url, ativo)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            data.get('titulo',''), data.get('slug',''),
            data.get('subtitulo',''),
            data.get('autor',''), data.get('conteudo',''),
            data.get('imagem_url',''), data.get('ativo', True)
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()

@app.route('/api/admin/blog/<int:post_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_post(post_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if request.method == 'DELETE':
            cur.execute("DELETE FROM posts WHERE id = %s", (post_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})
        data = request.get_json()
        cur.execute("""
            UPDATE posts SET titulo=%s, slug=%s, subtitulo=%s, autor=%s, conteudo=%s, imagem_url=%s, ativo=%s
            WHERE id=%s
        """, (
            data.get('titulo',''), data.get('slug',''),
            data.get('subtitulo',''),
            data.get('autor',''), data.get('conteudo',''),
            data.get('imagem_url',''), data.get('ativo', True), post_id
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — PLANOS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/planos', methods=['GET'])
@login_required
def api_admin_planos():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM planos ORDER BY preco")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API — QUIZ / COMPETIÇÃO DE PRATOS (salvar lead)
# ════════════════════════════════════════════════════════════

@app.route('/api/quiz_resultados', methods=['POST'])
def api_quiz_resultados():
    conn = None
    try:
        data              = request.get_json()
        nome              = (data.get('nome') or '').strip()
        email             = (data.get('email') or '').strip()
        categoria_slug    = (data.get('categoria_slug') or '').strip()
        pratos_escolhidos = data.get('pratos_escolhidos') or ''

        if not nome:
            return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400

        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO quiz_resultados (nome, email, categoria_slug, pratos_escolhidos)
            VALUES (%s, %s, %s, %s)
        """, (nome, email, categoria_slug, pratos_escolhidos))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': 'Erro ao salvar resultado'}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — QUIZ RESULTADOS (requer login)
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/quiz_resultados', methods=['GET'])
@login_required
def api_admin_quiz_resultados():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM quiz_resultados ORDER BY criado_em DESC")
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/quiz_resultados/<int:qr_id>', methods=['DELETE'])
@login_required
def api_admin_quiz_resultado(qr_id):
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM quiz_resultados WHERE id = %s", (qr_id,))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — AVALIAÇÕES DE PRATOS (requer login)
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/avaliacoes', methods=['GET'])
@login_required
def api_admin_avaliacoes():
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.nome as prato_nome, c.nome as categoria_nome,
                   ROUND(AVG(a.nota)::numeric, 1) as media,
                   COUNT(*) as total_votos
            FROM avaliacoes_pratos a
            JOIN pratos p ON a.prato_id = p.id
            LEFT JOIN categorias c ON p.categoria_id = c.id
            GROUP BY p.id, p.nome, c.nome
            ORDER BY media DESC, total_votos DESC
        """)
        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API PÚBLICA — BANNERS
# ════════════════════════════════════════════════════════════

@app.route('/api/banners')
def api_banners():
    """Retorna banners ativos para uma posição, já filtrados pela segmentação.
    Ex: /api/banners?posicao=categoria&categoria=carnes&cidade=São Paulo
    Um banner sem categoria/cidade/bairro definidos aparece em tudo daquela posição;
    se o admin preencher algum desses campos, o banner só aparece quando bater."""
    conn = None
    try:
        posicao        = request.args.get('posicao', 'home')
        categoria_slug = request.args.get('categoria', '').strip()
        cidade         = request.args.get('cidade', '').strip()
        bairro         = request.args.get('bairro', '').strip()
        prato_slug     = request.args.get('prato', '').strip()

        categoria_norm = gerar_slug(categoria_slug)
        prato_norm     = gerar_slug(prato_slug)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # OBS: o filtro de categoria/prato NÃO é feito aqui no SQL (c.slug = %s),
        # porque o campo categorias.slug é digitado à mão no admin e pode não bater
        # 100% (acento, maiúscula, espaço, hífen) com o slug usado nas páginas
        # públicas — isso fazia banners existentes sumirem sem erro nenhum.
        # Trazemos os candidatos e comparamos em Python com gerar_slug() dos dois lados.
        cur.execute("""
            SELECT b.id, b.titulo, b.imagem_url, b.link_destino,
                   (b.prato_id IS NOT NULL) AS especifico_do_prato,
                   b.categoria_id, c.slug AS categoria_slug_db, c.nome AS categoria_nome_db,
                   b.prato_id, p.slug AS prato_slug_db
            FROM banners b
            LEFT JOIN categorias c ON b.categoria_id = c.id
            LEFT JOIN pratos p ON b.prato_id = p.id
            WHERE b.ativo = TRUE
              AND (b.posicao = %s OR b.posicao = 'todas')
              AND (b.cidade IS NULL OR b.cidade = '' OR LOWER(b.cidade) = LOWER(%s))
              AND (b.bairro IS NULL OR b.bairro = '' OR LOWER(b.bairro) = LOWER(%s))
            ORDER BY b.ordem ASC, b.criado_em DESC
        """, (posicao, cidade, bairro))
        candidatos = [dict(r) for r in cur.fetchall()]
        cur.close()

        rows = []
        for r in candidatos:
            if r['categoria_id'] is not None:
                slug_db = gerar_slug(r.get('categoria_slug_db') or '')
                nome_db = gerar_slug(r.get('categoria_nome_db') or '')
                if not categoria_norm or categoria_norm not in (slug_db, nome_db):
                    continue
            if r['prato_id'] is not None:
                prato_slug_db = gerar_slug(r.get('prato_slug_db') or '')
                if not prato_norm or prato_norm != prato_slug_db:
                    continue
            rows.append(r)

        # Banner exclusivo de um prato específico "vence" os genéricos/por categoria
        # nessa mesma posição — é o que justifica cobrar separado por ele.
        exclusivos = [r for r in rows if r['especifico_do_prato']]
        rows = exclusivos or rows
        for r in rows:
            for campo in ('especifico_do_prato', 'categoria_id', 'categoria_slug_db',
                          'categoria_nome_db', 'prato_id', 'prato_slug_db'):
                r.pop(campo, None)

        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify([]), 500
    finally:
        if conn: conn.close()


# ════════════════════════════════════════════════════════════
#  API ADMIN — BANNERS
# ════════════════════════════════════════════════════════════

@app.route('/api/admin/banners', methods=['GET', 'POST'])
@login_required
def api_admin_banners():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if request.method == 'GET':
            cur.execute("""
                SELECT b.*, c.nome as categoria_nome, c.slug as categoria_slug,
                       p.nome as prato_nome
                FROM banners b
                LEFT JOIN categorias c ON b.categoria_id = c.id
                LEFT JOIN pratos p ON b.prato_id = p.id
                ORDER BY b.posicao, b.ordem ASC, b.criado_em DESC
            """)
            rows = [format_db_data(dict(r)) for r in cur.fetchall()]
            cur.close()
            return jsonify(rows)
        data = request.get_json()
        cur.execute("""
            INSERT INTO banners (titulo, imagem_url, link_destino, posicao, ativo, ordem, categoria_id, cidade, bairro, prato_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (
            data.get('titulo', '').strip(),
            data.get('imagem_url', '').strip(),
            data.get('link_destino', '').strip() or None,
            data.get('posicao', 'home'),
            data.get('ativo', True),
            data.get('ordem', 0),
            data.get('categoria_id') or None,
            data.get('cidade', '').strip() or None,
            data.get('bairro', '').strip() or None,
            data.get('prato_id') or None,
        ))
        new_id = cur.fetchone()['id']
        conn.commit()
        cur.close()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/banners/<int:banner_id>', methods=['PUT', 'DELETE'])
@login_required
def api_admin_banner(banner_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if request.method == 'DELETE':
            cur.execute("DELETE FROM banners WHERE id = %s", (banner_id,))
            conn.commit()
            cur.close()
            return jsonify({'ok': True})
        data = request.get_json()
        cur.execute("""
            UPDATE banners SET
                titulo = %s, imagem_url = %s, link_destino = %s,
                posicao = %s, ativo = %s, ordem = %s,
                categoria_id = %s, cidade = %s, bairro = %s, prato_id = %s
            WHERE id = %s
        """, (
            data.get('titulo', '').strip(),
            data.get('imagem_url', '').strip(),
            data.get('link_destino', '').strip() or None,
            data.get('posicao', 'home'),
            data.get('ativo', True),
            data.get('ordem', 0),
            data.get('categoria_id') or None,
            data.get('cidade', '').strip() or None,
            data.get('bairro', '').strip() or None,
            data.get('prato_id') or None,
            banner_id,
        ))
        conn.commit()
        cur.close()
        return jsonify({'ok': True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/banners/upload', methods=['POST'])
@login_required
def api_admin_banner_upload():
    """Recebe imagem base64, faz upload pro Cloudinary e retorna a URL."""
    import urllib.request as _urllib
    import json as _json
    import hashlib
    import time

    try:
        data       = request.get_json()
        image_data = data.get('image', '')
        if not image_data:
            return jsonify({'error': 'Imagem não enviada'}), 400

        # Remove prefixo data:image/xxx;base64, se existir
        if ',' in image_data:
            image_data = image_data.split(',')[1]

        cloud_name = os.getenv('CLOUDINARY_CLOUD_NAME')
        api_key    = os.getenv('CLOUDINARY_API_KEY')
        api_secret = os.getenv('CLOUDINARY_API_SECRET')

        if not all([cloud_name, api_key, api_secret]):
            return jsonify({'error': 'Cloudinary não configurado no .env'}), 500

        timestamp = str(int(time.time()))
        folder    = 'guiadorodizio_banners'
        params_to_sign = f"folder={folder}&timestamp={timestamp}{api_secret}"
        signature = hashlib.sha1(params_to_sign.encode()).hexdigest()

        boundary = 'BannerUploadBoundary'
        body_parts = []
        fields = {
            'file':      f'data:image/jpeg;base64,{image_data}',
            'api_key':   api_key,
            'timestamp': timestamp,
            'folder':    folder,
            'signature': signature,
        }
        for key, val in fields.items():
            body_parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{val}'.encode()
            )
        body_parts.append(f'--{boundary}--'.encode())
        body = b'\r\n'.join(body_parts)

        url = f'https://api.cloudinary.com/v1_1/{cloud_name}/image/upload'
        req = _urllib.Request(url, data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with _urllib.urlopen(req) as resp:
            result = _json.loads(resp.read())

        return jsonify({'ok': True, 'url': result['secure_url']})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ════════════════════════════════════════════════════════════
#  STATIC FILES
# ════════════════════════════════════════════════════════════

@app.route('/<path:path>')
def serve_static(path):
    basename = os.path.basename(path)
    if '.' not in basename:
        return "Not Found", 404
    if os.path.exists(os.path.join('.', path)):
        return send_from_directory('.', path)
    return "Not Found", 404


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
