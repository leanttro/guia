import os
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
app.secret_key = os.getenv('SECRET_KEY')
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
        cur.execute("SELECT * FROM blog WHERE slug = %s AND ativo = TRUE", (slug,))
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
#  API — RESTAURANTES
# ════════════════════════════════════════════════════════════

@app.route('/api/restaurantes')
def api_restaurantes():
    conn = None
    try:
        categoria_slug = request.args.get('categoria')
        lat  = request.args.get('lat', type=float)
        lng  = request.args.get('lng', type=float)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Se tiver lat/lng, ordena por proximidade (fórmula Haversine aproximada)
        if lat and lng and categoria_slug:
            cur.execute("""
                SELECT r.*, c.nome as categoria_nome,
                    (6371 * acos(cos(radians(%s)) * cos(radians(r.lat)) *
                    cos(radians(r.lng) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(r.lat)))) AS distancia_km
                FROM restaurantes r
                JOIN categorias c ON r.categoria_id = c.id
                WHERE c.slug = %s AND r.ativo = TRUE AND r.lat IS NOT NULL
                ORDER BY distancia_km
            """, (lat, lng, lat, categoria_slug))
        elif lat and lng:
            cur.execute("""
                SELECT r.*, c.nome as categoria_nome,
                    (6371 * acos(cos(radians(%s)) * cos(radians(r.lat)) *
                    cos(radians(r.lng) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(r.lat)))) AS distancia_km
                FROM restaurantes r
                JOIN categorias c ON r.categoria_id = c.id
                WHERE r.ativo = TRUE AND r.lat IS NOT NULL
                ORDER BY distancia_km
            """, (lat, lng, lat))
        elif categoria_slug:
            cur.execute("""
                SELECT r.*, c.nome as categoria_nome
                FROM restaurantes r
                JOIN categorias c ON r.categoria_id = c.id
                WHERE c.slug = %s AND r.ativo = TRUE
                ORDER BY r.nome
            """, (categoria_slug,))
        else:
            cur.execute("""
                SELECT r.*, c.nome as categoria_nome
                FROM restaurantes r
                JOIN categorias c ON r.categoria_id = c.id
                WHERE r.ativo = TRUE ORDER BY r.nome
            """)

        rows = [format_db_data(dict(r)) for r in cur.fetchall()]
        cur.close()
        return jsonify(rows)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar restaurantes'}), 500
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
#  API — BLOG
# ════════════════════════════════════════════════════════════

@app.route('/api/blog')
def api_blog():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM blog WHERE ativo = TRUE ORDER BY criado_em DESC")
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
        cur.execute("SELECT slug FROM blog WHERE ativo = TRUE AND slug IS NOT NULL")
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
