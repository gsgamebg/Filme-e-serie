import sqlite3
import requests
import json
import re
import time
import logging
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template_string
from urllib.parse import quote, urljoin, urlparse

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

app = Flask(__name__)

# ── 1. CONFIGURAÇÕES & CONSTANTES ─────────────────────────────────────────
DB_NAME = "historico.db"
UPSTREAM_BASE = "https://www.mendigoflix.lol"
ALLOWED_HOSTS = {'www.mendigoflix.lol', 'mendigoflix.lol'}
ALLOWED_PROFILES = {'maria_clara', 'convidado', 'kids'}
ALLOWED_KINDS = {'movie', 'series', 'anime'}
ALLOWED_SERVERS = {'servidor-1', 'servidor-2', 'servidor-3', 'servidor-4'}

# Sessão HTTP Reutilizável com pooling otimizado
HTTP = requests.Session()
HTTP.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8'
})

# ── 2. BANCO DE DADOS E CONEXÃO SEGURA ────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA busy_timeout = 5000')
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS progress (
                profile_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                title TEXT NOT NULL,
                poster_url TEXT,
                kind TEXT,
                tmdb_id TEXT,
                imdb_id TEXT,
                season INTEGER DEFAULT 1,
                episode INTEGER DEFAULT 1,
                time_seconds REAL DEFAULT 0,
                duration_seconds REAL DEFAULT 0,
                server TEXT DEFAULT 'servidor-1',
                completed INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (profile_id, item_id)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS favorites (
                profile_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                title TEXT NOT NULL,
                poster_url TEXT,
                kind TEXT,
                href TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (profile_id, item_id)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS cache (
                cache_key TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
        ''')

        conn.execute('CREATE INDEX IF NOT EXISTS idx_progress_profile_updated ON progress(profile_id, updated_at DESC);')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_progress_continue ON progress(profile_id, completed, updated_at DESC);')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_favorites_profile_created ON favorites(profile_id, created_at DESC);')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cache_updated ON cache(updated_at);')

init_db()

# ── 3. GERENCIADOR DE CACHE OTIMIZADO ─────────────────────────────────────
_last_cache_cleanup = 0

def maybe_clean_cache(interval=600, ttl_seconds=3600):
    global _last_cache_cleanup
    now = int(time.time())
    if now - _last_cache_cleanup < interval:
        return
    
    try:
        threshold = now - ttl_seconds
        with get_db() as conn:
            conn.execute('DELETE FROM cache WHERE updated_at < ?', (threshold,))
        _last_cache_cleanup = now
    except Exception as e:
        logging.error(f"Erro ao executar limpeza ocasional do cache: {e}")

def get_cached_data(key, ttl_seconds=900):
    maybe_clean_cache()
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT data_json, updated_at FROM cache WHERE cache_key = ?', (key,))
            row = cursor.fetchone()
            if row and (int(time.time()) - row['updated_at']) < ttl_seconds:
                return json.loads(row['data_json'])
    except Exception as e:
        logging.error(f"Erro na leitura do cache ({key}): {e}")
    return None

def set_cached_data(key, data):
    try:
        with get_db() as conn:
            conn.execute('''
                INSERT INTO cache (cache_key, data_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at
            ''', (key, json.dumps(data), int(time.time())))
    except Exception as e:
        logging.error(f"Erro ao gravar no cache ({key}): {e}")

# ── 4. REQUISIÇÕES HTTP E EXTRATORES ROBUSTOS ─────────────────────────────
def safe_http_get(url):
    res = HTTP.get(url, timeout=(3.05, 10))
    res.raise_for_status()
    
    final_host = urlparse(res.url).hostname
    if final_host not in ALLOWED_HOSTS:
        raise ValueError(f"Redirecionamento não permitido para host externo: {final_host}")
    return res

def determine_kind(href, fallback_type="", badge_text=""):
    href_str = (href or "").lower()
    badge_str = (badge_text or "").lower()
    
    if href_str.startswith('/anime/'):
        return 'anime'
    if href_str.startswith('/filme/') or 'filme' in badge_str or fallback_type == 'Movie':
        return 'movie'
    return 'series'

def extract_ep_json(html_text):
    match = re.search(r'(?:const|var|let)\s+EP\s*=\s*(\{)', html_text)
    if not match:
        return None
    
    start_idx = match.start(1)
    brace_count = 0
    in_string = False
    escape = False

    for i in range(start_idx, len(html_text)):
        char = html_text[i]
        if escape:
            escape = False
            continue
        if char == '\\':
            escape = True
            continue
        if char in ('"', "'"):
            if not in_string:
                in_string = char
            elif in_string == char:
                in_string = False
            continue
        if not in_string:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    json_str = html_text[start_idx:i+1]
                    try:
                        return json.loads(json_str)
                    except Exception as e:
                        logging.error(f"Falha ao decodificar JSON do EP: {e}")
                        return None
    return None

def parse_episodes_from_dom(soup):
    play_btns = soup.select('button[data-season][data-ep], button.bd-play')
    if not play_btns:
        return []
    
    seasons_dict = {}
    for btn in play_btns:
        try:
            s_num = int(btn.get('data-season', 1))
            e_num = int(btn.get('data-ep', 1))
            base_url = btn.get('data-base-url', '')
            url = btn.get('data-url', '')
            clean_base = re.sub(r'/servidor-\d+/?$', '', base_url or url)
            has_dub = btn.get('data-has-dub') == '1'
            has_leg = btn.get('data-has-leg') == '1'
            
            if s_num not in seasons_dict:
                seasons_dict[s_num] = {}
                
            seasons_dict[s_num][e_num] = {
                'number': e_num,
                'title': f"Episódio {e_num}",
                'base_url': clean_base,
                'player_url': url or (clean_base + "/servidor-1" if clean_base else ""),
                'has_dub': has_dub,
                'has_leg': has_leg
            }
        except Exception:
            continue
        
    seasons_list = []
    for s_num in sorted(seasons_dict.keys()):
        episodes = [seasons_dict[s_num][e] for e in sorted(seasons_dict[s_num].keys())]
        seasons_list.append({
            'season_number': s_num,
            'episodes': episodes
        })
    return seasons_list

def parse_catalog_html(html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    items = []
    
    cards = soup.select('a.card, .grid a[href*="/filme/"], .grid a[href*="/serie/"], .grid a[href*="/anime/"], a.rec-card')
    seen_hrefs = set()

    for card in cards:
        href = card.get('href', '')
        if not href or href in seen_hrefs:
            continue
        
        seen_hrefs.add(href)
        title = card.get('title') or (card.select_one('.card-title, .rec-title') and card.select_one('.card-title, .rec-title').get_text(strip=True)) or ''
        
        img = card.select_one('img.card-img, img.poster, img.rec-thumb, img')
        poster = ''
        if img:
            poster = img.get('data-src') or img.get('data-lazy-src') or img.get('src') or ''

        badge = card.select_one('.badge-kind')
        badge_text = badge.get_text(strip=True) if badge else ''

        kind = determine_kind(href, badge_text=badge_text)

        if title:
            items.append({
                "id": href,
                "title": title,
                "poster": poster,
                "type": kind,
                "href": href
            })

    has_next = bool(soup.select_one('a[rel="next"], .pagination .next, a.next, link[rel="next"]'))
    return items, has_next

def parse_catalog_json(data_json):
    items = []
    raw_items = data_json.get('items', []) if isinstance(data_json, dict) else []
    for it in raw_items:
        href = it.get('href', '')
        items.append({
            "id": href or it.get('title', ''),
            "title": it.get('title', ''),
            "poster": it.get('poster', ''),
            "type": determine_kind(href, fallback_type=it.get('type', '')),
            "href": href
        })
    return items

def fetch_catalog(category="todos", page=1, query=None):
    norm_query = ' '.join((query or '').strip().lower().split())[:100] if query else None
    cache_key = f"cat_{category}_{page}_{norm_query}"
    
    cached = get_cached_data(cache_key)
    if cached:
        return cached

    items = []
    has_next = False

    try:
        if norm_query:
            url = f"{UPSTREAM_BASE}/buscar?q={quote(norm_query)}"
        else:
            cat_map = {
                "filmes": "/filmes",
                "series": "/series",
                "animes": "/animes",
                "lancamentos": "/lancamentos?f=todos"
            }
            path = cat_map.get(category, "/lancamentos?f=todos")
            delimiter = "&" if "?" in path else "?"
            url = f"{UPSTREAM_BASE}{path}{delimiter}page={page}"

        res = safe_http_get(url)
        content_type = res.headers.get("Content-Type", "").lower()

        if "application/json" in content_type:
            try:
                items = parse_catalog_json(res.json())
                has_next = len(items) >= 20
            except Exception:
                items, has_next = parse_catalog_html(res.text)
        else:
            items, has_next = parse_catalog_html(res.text)

        result = {"items": items, "page": page, "has_next": has_next}
        if items:
            set_cached_data(cache_key, result)
        return result
    except Exception as e:
        logging.error(f"Erro ao buscar catálogo upstream: {e}")
        return {"items": [], "page": page, "has_next": False}

def fetch_details(href):
    cache_key = f"details_{href}"
    cached = get_cached_data(cache_key, ttl_seconds=1800) # 30 min
    if cached:
        return cached

    kind_by_url = determine_kind(href)

    details = {
        'id': href,
        'title': '',
        'synopsis': '',
        'poster': '',
        'type': kind_by_url,
        'tmdb_id': None,
        'imdb_id': None,
        'runtime': None,
        'genres': [],
        'cast': [],
        'director': [],
        'seasons': [],
        'movie_servers': []
    }

    try:
        url = urljoin(UPSTREAM_BASE, href)
        res = safe_http_get(url)
        html = res.text
        soup = BeautifulSoup(html, 'html.parser')

        # 1. Metadados JSON-LD
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get('@type') in ['Movie', 'TVSeries', 'CreativeWorkSeries']:
                    details['title'] = data.get('name', '')
                    details['synopsis'] = data.get('description', '')
                    details['poster'] = data.get('image', '')
                    details['genres'] = data.get('genre', [])
                    
                    crew_raw = data.get('director') or data.get('creator') or []
                    if isinstance(crew_raw, dict): crew_raw = [crew_raw]
                    details['director'] = [p.get('name') for p in crew_raw if isinstance(p, dict) and p.get('name')]

                    actor_raw = data.get('actor') or []
                    if isinstance(actor_raw, dict): actor_raw = [actor_raw]
                    details['cast'] = [a.get('name') for a in actor_raw if isinstance(a, dict) and a.get('name')]

                    for same in data.get('sameAs', []):
                        if 'themoviedb.org' in same:
                            m = re.search(r'/(movie|tv)/(\d+)', same)
                            if m: details['tmdb_id'] = m.group(2)
                        elif 'imdb.com' in same:
                            m = re.search(r'/title/(tt\d+)', same)
                            if m: details['imdb_id'] = m.group(1)
                    break
            except Exception:
                continue

        # Fallbacks via DOM
        if not details['title']:
            t_el = soup.select_one('.title, h1')
            if t_el: details['title'] = t_el.get_text(strip=True)

        if not details['synopsis']:
            s_el = soup.select_one('.syn, .synopsis')
            if s_el: details['synopsis'] = s_el.get_text(strip=True)

        r_el = soup.select_one('.runtime')
        if r_el: details['runtime'] = r_el.get_text(strip=True).replace('•', '').strip()

        if not details['poster']:
            p_el = soup.select_one('img.poster, .header img')
            if p_el: details['poster'] = p_el.get('src', '')

        # 2. Extração Fiel de Episódios
        ep_json = extract_ep_json(html)
        if ep_json and isinstance(ep_json, dict):
            seasons_list = []
            for season_num, ep_array in ep_json.items():
                episodes = []
                if isinstance(ep_array, list):
                    for ep in ep_array:
                        if isinstance(ep, dict):
                            base = ep.get('baseUrl', '')
                            actual_url = ep.get('url', '')
                            clean_base = re.sub(r'/servidor-\d+/?$', '', base or actual_url)
                            
                            episodes.append({
                                'number': int(ep.get('n', 0)),
                                'title': ep.get('title', f"Episódio {ep.get('n')}"),
                                'base_url': clean_base,
                                'player_url': actual_url or (clean_base + "/servidor-1" if clean_base else ""),
                                'has_dub': bool(ep.get('hasDub', False)),
                                'has_leg': bool(ep.get('hasLeg', False))
                            })
                    if episodes:
                        try:
                            s_int = int(season_num)
                        except ValueError:
                            s_int = 1
                        seasons_list.append({
                            'season_number': s_int,
                            'episodes': sorted(episodes, key=lambda x: x['number'])
                        })
            details['seasons'] = sorted(seasons_list, key=lambda x: x['season_number'])

        if not details['seasons']:
            details['seasons'] = parse_episodes_from_dom(soup)

        # 3. Servidores de Filme
        server_btns = soup.select('.server-btn')
        servers = []
        for btn in server_btns:
            srv_name = btn.get('data-server') or 'servidor-1'
            srv_label = btn.get_text(strip=True) or srv_name
            src = btn.get('data-src') or ''
            servers.append({'server': srv_name, 'label': srv_label, 'src': src})

        player_iframe = soup.select_one('iframe#player, iframe')
        if player_iframe and player_iframe.get('src'):
            iframe_src = player_iframe.get('src')
            if not details['tmdb_id']:
                m = re.search(r'fembed\.sx/e/(\d+)', iframe_src)
                if m: details['tmdb_id'] = m.group(1)
            
            if not servers:
                servers.append({
                    'server': 'servidor-1',
                    'label': 'Principal',
                    'src': iframe_src
                })

        details['movie_servers'] = servers

        set_cached_data(cache_key, details)
    except Exception as e:
        logging.error(f"Erro ao buscar detalhes ({href}): {e}")

    return details

# ── 5. ROTAS DA API REST ──────────────────────────────────────────────────
@app.route('/api/catalog')
def api_catalog():
    cat = request.args.get('cat', 'todos')
    page = request.args.get('page', 1, type=int)
    return jsonify(fetch_catalog(category=cat, page=page))

@app.route('/api/featured')
def api_featured():
    catalog = fetch_catalog(category="todos", page=1)
    items = catalog.get("items", [])
    valid = [it for it in items if it.get("poster") and it.get("title")]
    return jsonify(valid[:5])

@app.route('/api/search')
def api_search():
    q = request.args.get('q', '')
    if not q: return jsonify({"items": [], "page": 1, "has_next": False})
    return jsonify(fetch_catalog(query=q))

@app.route('/api/details')
def api_details():
    href = request.args.get('href', '')
    if not href: return jsonify({'error': 'href obrigatório'}), 400
    return jsonify(fetch_details(href))

@app.route('/api/progress/save', methods=['POST'])
def api_save_progress():
    data = request.json or {}
    
    profile_id = data.get('profile_id', 'maria_clara')
    if profile_id not in ALLOWED_PROFILES:
        profile_id = 'maria_clara'

    item_id = (data.get('item_id') or '').strip()
    if not item_id:
        return jsonify({'error': 'item_id é obrigatório'}), 400

    title = (data.get('title') or 'Sem título').strip()
    poster_url = (data.get('poster_url') or '').strip()
    
    kind = data.get('kind', 'movie')
    if kind not in ALLOWED_KINDS:
        kind = 'movie'

    server = data.get('server', 'servidor-1')
    if server not in ALLOWED_SERVERS:
        server = 'servidor-1'

    tmdb_id = str(data.get('tmdb_id') or '').strip()
    imdb_id = str(data.get('imdb_id') or '').strip()
    season = max(1, int(data.get('season', 1)))
    episode = max(1, int(data.get('episode', 1)))
    time_seconds = max(0.0, float(data.get('time_seconds', 0)))
    duration_seconds = max(0.0, float(data.get('duration_seconds', 0)))
    completed = 1 if data.get('completed') else 0

    with get_db() as conn:
        conn.execute('''
            INSERT INTO progress (profile_id, item_id, title, poster_url, kind, tmdb_id, imdb_id, season, episode, time_seconds, duration_seconds, server, completed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(profile_id, item_id) DO UPDATE SET
                title = excluded.title,
                poster_url = excluded.poster_url,
                kind = excluded.kind,
                tmdb_id = excluded.tmdb_id,
                imdb_id = excluded.imdb_id,
                season = excluded.season,
                episode = excluded.episode,
                time_seconds = excluded.time_seconds,
                duration_seconds = excluded.duration_seconds,
                server = excluded.server,
                completed = excluded.completed,
                updated_at = CURRENT_TIMESTAMP
        ''', (profile_id, item_id, title, poster_url, kind, tmdb_id, imdb_id, season, episode, time_seconds, duration_seconds, server, completed))

    return jsonify({'status': 'ok'})

@app.route('/api/progress/<path:item_id>')
def api_get_progress(item_id):
    profile_id = request.args.get('profile_id', 'maria_clara')
    if profile_id not in ALLOWED_PROFILES: profile_id = 'maria_clara'

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT season, episode, time_seconds, server, completed FROM progress WHERE profile_id = ? AND item_id = ?', (profile_id, item_id))
        row = cursor.fetchone()

        if row:
            return jsonify({
                'item_id': item_id,
                'season': row['season'],
                'episode': row['episode'],
                'time_seconds': row['time_seconds'],
                'server': row['server'],
                'completed': bool(row['completed'])
            })
    return jsonify({'item_id': item_id, 'season': 1, 'episode': 1, 'time_seconds': 0, 'server': 'servidor-1', 'completed': False})

@app.route('/api/continue-watching')
def api_continue_watching():
    profile_id = request.args.get('profile_id', 'maria_clara')
    if profile_id not in ALLOWED_PROFILES: profile_id = 'maria_clara'

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT item_id, title, poster_url, kind, season, episode, time_seconds, duration_seconds 
            FROM progress 
            WHERE profile_id = ? AND completed = 0 AND (time_seconds > 5 OR season > 1 OR episode > 1)
            ORDER BY updated_at DESC LIMIT 12
        ''', (profile_id,))
        rows = cursor.fetchall()

        result = []
        for r in rows:
            pct = 0
            if r['duration_seconds'] and r['duration_seconds'] > 0:
                pct = min(100, int((r['time_seconds'] / r['duration_seconds']) * 100))
            elif r['time_seconds'] > 0:
                pct = 35
            result.append({
                'id': r['item_id'],
                'title': r['title'],
                'poster': r['poster_url'],
                'type': r['kind'],
                'href': r['item_id'],
                'season': r['season'],
                'episode': r['episode'],
                'progress_percent': pct
            })
    return jsonify(result)

@app.route('/api/progress/delete', methods=['POST'])
def api_delete_progress():
    data = request.json or {}
    profile_id = data.get('profile_id', 'maria_clara')
    item_id = data.get('item_id')
    
    with get_db() as conn:
        conn.execute('DELETE FROM progress WHERE profile_id = ? AND item_id = ?', (profile_id, item_id))
    return jsonify({'status': 'deleted'})

@app.route('/api/favorites/toggle', methods=['POST'])
def api_toggle_favorite():
    data = request.json or {}
    profile_id = data.get('profile_id', 'maria_clara')
    if profile_id not in ALLOWED_PROFILES: profile_id = 'maria_clara'
    
    item_id = data.get('item_id')
    if not item_id: return jsonify({'error': 'item_id obrigatório'}), 400

    title = data.get('title', '')
    poster_url = data.get('poster_url', '')
    kind = data.get('kind', 'movie')
    href = data.get('href', '')

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM favorites WHERE profile_id = ? AND item_id = ?', (profile_id, item_id))
        exists = cursor.fetchone()

        if exists:
            conn.execute('DELETE FROM favorites WHERE profile_id = ? AND item_id = ?', (profile_id, item_id))
            is_fav = False
        else:
            conn.execute('''
                INSERT INTO favorites (profile_id, item_id, title, poster_url, kind, href)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (profile_id, item_id, title, poster_url, kind, href))
            is_fav = True

    return jsonify({'is_favorite': is_fav})

@app.route('/api/favorites')
def api_get_favorites():
    profile_id = request.args.get('profile_id', 'maria_clara')
    if profile_id not in ALLOWED_PROFILES: profile_id = 'maria_clara'

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT item_id, title, poster_url, kind, href FROM favorites WHERE profile_id = ? ORDER BY created_at DESC', (profile_id,))
        rows = cursor.fetchall()

        result = [{
            'id': r['item_id'],
            'title': r['title'],
            'poster': r['poster_url'],
            'type': r['kind'],
            'href': r['href']
        } for r in rows]
    return jsonify(result)

# ── 6. FRONTEND SINGLE PAGE (HTML + CSS + JS ULTRA-MODERNO) ────────────────
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes">
    <meta name="theme-color" content="#070913">
    <title>CineFlix — Streaming de Alta Performance</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:ital,wght@0,400;0,500;0,600;0,700;0,800;1,600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #070913;
            --bg-surface: #0f1424;
            --bg-card: #141b2d;
            --bg-card-hover: #1c263e;
            --accent-primary: #6366f1;
            --accent-pink: #ec4899;
            --accent-cyan: #06b6d4;
            --accent-gradient: linear-gradient(135deg, #6366f1 0%, #a855f7 50%, #ec4899 100%);
            --accent-glow: rgba(99, 102, 241, 0.35);
            --text-bright: #f8fafc;
            --text-muted: #94a3b8;
            --text-dim: #64748b;
            --border-color: rgba(255, 255, 255, 0.08);
            --border-highlight: rgba(99, 102, 241, 0.4);
            --radius-sm: 8px;
            --radius-md: 14px;
            --radius-lg: 20px;
            --radius-full: 9999px;
            --shadow-glow: 0 10px 30px -10px rgba(99, 102, 241, 0.5);
            --transition-fast: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            --transition-smooth: all 0.35s cubic-bezier(0.16, 1, 0.3, 1);
        }

        * { margin:0; padding:0; box-sizing:border-box; font-family:'Plus Jakarta Sans', -apple-system, sans-serif; }
        
        body {
            background-color: var(--bg-dark);
            color: var(--text-bright);
            min-height: 100vh;
            overflow-x: hidden;
            background-image: 
                radial-gradient(circle at 15% 15%, rgba(99, 102, 241, 0.08) 0%, transparent 45%),
                radial-gradient(circle at 85% 85%, rgba(236, 72, 153, 0.06) 0%, transparent 45%);
            background-attachment: fixed;
        }

        /* Scrollbar personalizada */
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: var(--bg-dark); }
        ::-webkit-scrollbar-thumb { background: #222e48; border-radius: var(--radius-full); }
        ::-webkit-scrollbar-thumb:hover { background: var(--accent-primary); }

        /* HEADER GLASSMORPHIC */
        header {
            position: sticky; top:0; z-index: 100;
            background: rgba(7, 9, 19, 0.82);
            backdrop-filter: blur(16px) saturate(180%);
            -webkit-backdrop-filter: blur(16px) saturate(180%);
            border-bottom: 1px solid var(--border-color);
            padding: 12px 24px;
            display: flex; align-items: center; justify-content: space-between; gap: 16px;
            transition: var(--transition-fast);
        }

        .brand {
            display: flex; align-items: center; gap: 10px; cursor: pointer; text-decoration: none;
        }
        .logo-icon {
            width: 38px; height: 38px; border-radius: var(--radius-md);
            background: var(--accent-gradient);
            display: flex; align-items: center; justify-content: center;
            box-shadow: var(--shadow-glow); font-size: 1.2rem;
            animation: pulse-glow 3s infinite alternate;
        }
        @keyframes pulse-glow {
            0% { box-shadow: 0 0 15px rgba(99, 102, 241, 0.4); }
            100% { box-shadow: 0 0 25px rgba(236, 72, 153, 0.6); }
        }
        .logo-text {
            font-size: 1.35rem; font-weight: 800; letter-spacing: -0.5px;
            background: var(--accent-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .logo-tag {
            font-size: 0.65rem; font-weight: 700; background: rgba(99, 102, 241, 0.2); color: #a5b4fc;
            padding: 2px 8px; border-radius: var(--radius-full); border: 1px solid rgba(99, 102, 241, 0.3);
        }

        /* HEADER ACTIONS */
        .header-actions { display: flex; align-items: center; gap: 12px; flex: 1; justify-content: flex-end; }
        
        .search-box {
            position: relative; display: flex; align-items: center; width: 100%; max-width: 320px;
        }
        .search-input {
            width: 100%; background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border-color);
            padding: 10px 38px 10px 38px; border-radius: var(--radius-full); color: #fff; font-size: 0.82rem;
            outline: none; transition: var(--transition-fast);
        }
        .search-input:focus {
            background: rgba(15, 20, 36, 0.95); border-color: var(--accent-primary);
            box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.15); width: 100%;
        }
        .search-icon-left { position: absolute; left: 14px; color: var(--text-dim); font-size: 0.9rem; pointer-events: none; }
        .search-shortcut {
            position: absolute; right: 12px; background: rgba(255, 255, 255, 0.1); border-radius: 4px;
            padding: 2px 6px; font-size: 0.65rem; color: var(--text-muted); font-weight: 700; pointer-events: none;
        }

        .profile-selector {
            background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border-color); color: #fff;
            padding: 8px 14px; border-radius: var(--radius-full); font-weight: 700; font-size: 0.8rem;
            outline: none; cursor: pointer; transition: var(--transition-fast); display: flex; align-items: center; gap: 6px;
        }
        .profile-selector:hover { border-color: var(--accent-primary); background: rgba(99, 102, 241, 0.15); }

        /* CONTAINER PRINCIPAL */
        main { max-width: 1500px; margin: 0 auto; padding: 20px 24px; }

        /* HERO BANNER DESTAQUE */
        .hero-banner {
            position: relative; width: 100%; min-height: 340px; border-radius: var(--radius-lg);
            overflow: hidden; margin-bottom: 30px; display: flex; align-items: flex-end;
            background: var(--bg-surface); border: 1px solid var(--border-color);
            box-shadow: 0 20px 40px rgba(0,0,0,0.5);
            transition: var(--transition-smooth);
        }
        .hero-bg {
            position: absolute; inset:0; background-size: cover; background-position: center 20%;
            filter: brightness(0.55) contrast(1.1); transform: scale(1.02); transition: var(--transition-smooth);
        }
        .hero-overlay {
            position: absolute; inset:0;
            background: linear-gradient(0deg, var(--bg-dark) 0%, rgba(7,9,19,0.7) 50%, transparent 100%),
                        linear-gradient(90deg, var(--bg-dark) 0%, transparent 70%);
        }
        .hero-content {
            position: relative; z-index: 2; padding: 36px; max-width: 650px; display: flex; flex-direction: column; gap: 12px;
        }
        .hero-badge {
            align-self: flex-start; background: var(--accent-gradient); color: #fff; font-size: 0.7rem;
            font-weight: 800; text-transform: uppercase; letter-spacing: 1px; padding: 4px 12px; border-radius: var(--radius-full);
            box-shadow: 0 4px 12px rgba(99,102,241,0.4);
        }
        .hero-title { font-size: 2.2rem; font-weight: 800; line-height: 1.15; letter-spacing: -0.5px; text-shadow: 0 2px 10px rgba(0,0,0,0.8); }
        .hero-desc { font-size: 0.88rem; color: var(--text-muted); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
        .hero-btns { display: flex; gap: 12px; margin-top: 8px; flex-wrap: wrap; }

        /* TABS DE NAVEGAÇÃO */
        .nav-tabs {
            display: flex; gap: 10px; margin-bottom: 28px; overflow-x: auto; padding: 4px 0 8px 0;
            scroll-behavior: smooth; -webkit-overflow-scrolling: touch;
        }
        .tab-btn {
            background: rgba(255, 255, 255, 0.04); border: 1px solid var(--border-color); color: var(--text-muted);
            padding: 10px 20px; border-radius: var(--radius-full); font-weight: 700; font-size: 0.85rem;
            cursor: pointer; white-space: nowrap; flex-shrink: 0; transition: var(--transition-fast);
            display: flex; align-items: center; gap: 8px;
        }
        .tab-btn:hover { background: rgba(255, 255, 255, 0.08); color: #fff; border-color: rgba(255, 255, 255, 0.2); }
        .tab-btn.active {
            background: var(--accent-gradient); color: #fff; border-color: transparent;
            box-shadow: var(--shadow-glow);
        }

        /* SEÇÃO DE TÍTULO */
        .section-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
        .section-title {
            font-size: 1.15rem; font-weight: 800; letter-spacing: -0.3px; display: flex; align-items: center; gap: 10px;
        }
        .section-title::before {
            content: ''; width: 4px; height: 18px; background: var(--accent-pink); border-radius: var(--radius-full);
            box-shadow: 0 0 10px var(--accent-pink);
        }

        /* GRID DE CARDS */
        .grid {
            display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 16px; margin-bottom: 32px;
        }

        /* CARD MODERNO COM GLASS & HOVER EFFECT */
        .card {
            background: var(--bg-card); border: 1px solid var(--border-color); border-radius: var(--radius-md);
            overflow: hidden; cursor: pointer; position: relative; transition: var(--transition-smooth);
            display: flex; flex-direction: column; will-change: transform;
        }
        .card:hover {
            transform: translateY(-8px) scale(1.02);
            border-color: var(--border-highlight);
            box-shadow: 0 14px 30px rgba(0, 0, 0, 0.6), 0 0 20px rgba(99, 102, 241, 0.25);
        }
        .card:active { transform: scale(0.97); }
        
        .poster-wrap { aspect-ratio: 2/3; width: 100%; background: #0b0f19; position: relative; overflow: hidden; }
        .poster-img { width: 100%; height: 100%; object-fit: cover; transition: var(--transition-smooth); }
        .card:hover .poster-img { transform: scale(1.08); filter: brightness(1.05); }

        .play-overlay {
            position: absolute; inset:0; background: rgba(7, 9, 19, 0.45); backdrop-filter: blur(2px);
            display: flex; align-items: center; justify-content: center; opacity: 0; transition: var(--transition-fast);
        }
        .card:hover .play-overlay { opacity: 1; }
        .play-icon-btn {
            width: 48px; height: 48px; border-radius: var(--radius-full); background: var(--accent-gradient);
            color: #fff; display: flex; align-items: center; justify-content: center; font-size: 1.2rem;
            box-shadow: 0 6px 20px rgba(0,0,0,0.5); transform: scale(0.8); transition: var(--transition-fast);
            padding-left: 3px;
        }
        .card:hover .play-icon-btn { transform: scale(1); }

        .card-badge {
            position: absolute; top: 8px; left: 8px; background: rgba(7, 9, 19, 0.85);
            backdrop-filter: blur(6px); padding: 4px 8px; border-radius: var(--radius-sm);
            font-size: 0.62rem; font-weight: 800; color: #a5b4fc; border: 1px solid var(--border-color); letter-spacing: 0.5px;
        }

        .card-info { padding: 12px; display: flex; flex-direction: column; gap: 4px; flex: 1; justify-content: space-between; }
        .card-title {
            font-size: 0.84rem; font-weight: 700; color: var(--text-bright); line-height: 1.3;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
        }

        /* PROGRESSO CONTINUAR ASSISTINDO */
        .progress-container { margin-top: 6px; }
        .progress-bar { width: 100%; height: 4px; background: rgba(255,255,255,0.1); border-radius: var(--radius-full); overflow: hidden; }
        .progress-fill { height: 100%; background: var(--accent-gradient); border-radius: var(--radius-full); }

        /* SKELETON ANIMADO */
        .skeleton {
            background: linear-gradient(90deg, #101626 25%, #1c263b 50%, #101626 75%);
            background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: var(--radius-md);
        }
        @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

        /* BOTÃO DE AÇÃO / PADRÃO */
        .btn-action {
            background: rgba(255, 255, 255, 0.07); border: 1px solid var(--border-color); color: #fff;
            padding: 10px 18px; border-radius: var(--radius-md); font-weight: 700; font-size: 0.82rem;
            cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 8px;
            white-space: nowrap; transition: var(--transition-fast); outline: none;
        }
        .btn-action:hover { background: rgba(255, 255, 255, 0.14); border-color: rgba(255, 255, 255, 0.25); transform: translateY(-2px); }
        .btn-action:active { transform: translateY(0); }
        .btn-primary { background: var(--accent-gradient); border-color: transparent; box-shadow: var(--shadow-glow); }
        .btn-primary:hover { opacity: 0.95; box-shadow: 0 10px 25px rgba(99, 102, 241, 0.6); }

        /* MODAL DE DETALHES & PLAYER */
        .modal {
            position: fixed; inset:0; z-index: 999; background: rgba(4, 6, 14, 0.92);
            backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
            display: none; align-items: center; justify-content: center; padding: 20px;
            animation: fadeIn 0.25s ease-out;
        }
        .modal.open { display: flex; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

        .modal-card {
            background: var(--bg-surface); border: 1px solid var(--border-highlight);
            width: 100%; max-width: 950px; max-height: 92vh; border-radius: var(--radius-lg);
            overflow-y: auto; box-shadow: 0 25px 60px rgba(0,0,0,0.8); display: flex; flex-direction: column;
            animation: scaleUp 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes scaleUp { from { transform: scale(0.92) translateY(20px); opacity: 0; } to { transform: scale(1) translateY(0); opacity: 1; } }

        .modal-header {
            position: sticky; top: 0; background: rgba(15, 20, 36, 0.95); backdrop-filter: blur(12px);
            z-index: 20; padding: 14px 20px; display: flex; justify-content: space-between; align-items: center;
            border-bottom: 1px solid var(--border-color); gap: 12px;
        }
        .modal-body { padding: 20px; display: flex; flex-direction: column; gap: 20px; }

        .details-header { display: flex; gap: 18px; flex-direction: row; }
        .details-poster { width: 130px; aspect-ratio: 2/3; border-radius: var(--radius-md); object-fit: cover; box-shadow: 0 8px 20px rgba(0,0,0,0.5); flex-shrink: 0; }
        .details-info { flex: 1; display: flex; flex-direction: column; gap: 8px; }
        .details-title { font-size: 1.4rem; font-weight: 800; letter-spacing: -0.3px; }
        .details-meta { font-size: 0.78rem; color: #a5b4fc; font-weight: 700; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .details-synopsis { font-size: 0.85rem; color: var(--text-muted); line-height: 1.55; margin-top: 4px; }

        /* ── CUSTOM VIDEO PLAYER ── */
        .player-container {
            position: relative; width: 100%; aspect-ratio: 16/9; background: #000;
            border-radius: var(--radius-md); overflow: hidden; box-shadow: 0 12px 35px rgba(0,0,0,0.7);
            border: 1px solid var(--border-color); user-select: none;
        }
        
        .player-video-elem { width: 100%; height: 100%; object-fit: contain; }
        .player-iframe-elem { width: 100%; height: 100%; border: 0; }

        /* Escudo Transparente para consumo de Popups no Iframe */
        .click-shield {
            position: absolute; inset: 0; z-index: 10; background: rgba(0,0,0,0.01);
            cursor: pointer; display: flex; align-items: center; justify-content: center;
        }
        .click-shield-btn {
            background: rgba(99, 102, 241, 0.9); color: #fff; padding: 10px 20px; border-radius: var(--radius-full);
            font-weight: 800; font-size: 0.85rem; box-shadow: 0 0 20px rgba(0,0,0,0.6); pointer-events: none;
            display: flex; align-items: center; gap: 8px; backdrop-filter: blur(4px);
        }

        /* OVERLAY DE CONTROLES CUSTOMIZADOS */
        .player-controls-overlay {
            position: absolute; inset: 0; z-index: 15;
            background: linear-gradient(180deg, rgba(0,0,0,0.7) 0%, transparent 20%, transparent 75%, rgba(0,0,0,0.85) 100%);
            display: flex; flex-direction: column; justify-content: space-between; padding: 16px;
            opacity: 0; transition: opacity 0.3s ease; pointer-events: none;
        }
        .player-container:hover .player-controls-overlay,
        .player-controls-overlay.active { opacity: 1; pointer-events: auto; }

        .player-top-bar { display: flex; justify-content: space-between; align-items: center; color: #fff; font-weight: 700; font-size: 0.9rem; }
        .player-title-text { text-shadow: 0 2px 8px rgba(0,0,0,0.8); }

        .player-bottom-bar { display: flex; flex-direction: column; gap: 8px; width: 100%; }

        /* TIMELINE / SEEK BAR */
        .player-seek-container { position: relative; width: 100%; height: 16px; display: flex; align-items: center; cursor: pointer; }
        .player-seek-bg { width: 100%; height: 5px; background: rgba(255,255,255,0.25); border-radius: 3px; position: relative; transition: height 0.15s; }
        .player-seek-container:hover .player-seek-bg { height: 8px; }
        .player-seek-progress { height: 100%; background: var(--accent-gradient); border-radius: 3px; width: 0%; position: relative; }
        .player-seek-handle {
            position: absolute; right: -6px; top: 50%; transform: translateY(-50%) scale(0);
            width: 14px; height: 14px; border-radius: 50%; background: #fff; box-shadow: 0 0 10px rgba(0,0,0,0.5);
            transition: transform 0.15s;
        }
        .player-seek-container:hover .player-seek-handle { transform: translateY(-50%) scale(1); }

        .player-buttons-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; color: #fff; }
        .player-left-ctrls, .player-right-ctrls { display: flex; align-items: center; gap: 12px; }

        .ctrl-btn {
            background: transparent; border: none; color: #f8fafc; font-size: 1.1rem; cursor: pointer;
            padding: 6px; border-radius: var(--radius-sm); transition: var(--transition-fast); display: flex; align-items: center; justify-content: center;
        }
        .ctrl-btn:hover { color: var(--accent-pink); transform: scale(1.15); background: rgba(255,255,255,0.1); }
        .time-display { font-size: 0.75rem; font-weight: 700; color: #cbd5e1; font-mono: monospace; }

        .volume-container { display: flex; align-items: center; gap: 6px; position: relative; }
        .volume-slider { width: 0; opacity: 0; transition: all 0.25s ease; accent-color: var(--accent-primary); cursor: pointer; }
        .volume-container:hover .volume-slider { width: 60px; opacity: 1; }

        /* GRID DE EPISÓDIOS */
        .ep-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(85px, 1fr)); gap: 8px; }
        .ep-btn {
            background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border-color); color: #cbd5e1;
            padding: 10px 8px; border-radius: var(--radius-md); text-align: center; font-size: 0.75rem;
            font-weight: 700; cursor: pointer; display: flex; flex-direction: column; align-items: center;
            justify-content: center; gap: 4px; transition: var(--transition-fast);
        }
        .ep-btn:hover { background: rgba(255,255,255,0.12); border-color: rgba(255,255,255,0.25); color: #fff; }
        .ep-btn.active { background: var(--accent-gradient); color: #fff; border-color: transparent; box-shadow: var(--shadow-glow); }
        .lang-badge { font-size: 0.62rem; padding: 2px 6px; border-radius: 4px; background: rgba(0,0,0,0.3); font-weight: 800; }

        /* TOAST NOTIFICAÇÃO */
        .toast-container { position: fixed; bottom: 24px; right: 24px; z-index: 1000; display: flex; flex-direction: column; gap: 8px; pointer-events: none; }
        .toast {
            background: rgba(15, 20, 36, 0.95); border: 1px solid var(--accent-primary); color: #fff;
            padding: 12px 20px; border-radius: var(--radius-md); font-weight: 700; font-size: 0.85rem;
            box-shadow: 0 10px 30px rgba(0,0,0,0.6); display: flex; align-items: center; gap: 10px;
            animation: slideInRight 0.3s cubic-bezier(0.16, 1, 0.3, 1); backdrop-filter: blur(10px);
        }
        @keyframes slideInRight { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

        footer {
            border-top: 1px solid var(--border-color); padding: 24px; text-align: center;
            color: var(--text-dim); font-size: 0.8rem; margin-top: 40px; background: rgba(7, 9, 19, 0.95);
        }
        footer strong { color: var(--accent-pink); }

        /* RESPONSIVIDADE FINA */
        @media (max-width: 768px) {
            header { padding: 10px 14px; }
            main { padding: 14px; }
            .hero-banner { min-height: 260px; }
            .hero-content { padding: 20px; }
            .hero-title { font-size: 1.5rem; }
            .grid { grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 12px; }
            .details-header { flex-direction: column; align-items: center; text-align: center; }
            .details-poster { width: 110px; }
            .details-meta { justify-content: center; }
            .modal-card { max-height: 96vh; }
        }
    </style>
</head>
<body>

    <header>
        <a href="#" class="brand" onclick="switchCategory('todos', document.querySelector('.tab-btn'))">
            <div class="logo-icon">🎬</div>
            <div class="logo-text">CineFlix</div>
            <span class="logo-tag">PRO</span>
        </a>

        <div class="header-actions">
            <div class="search-box">
                <span class="search-icon-left">🔍</span>
                <input type="text" id="searchInput" class="search-input" placeholder="Buscar filmes, séries, animes...">
                <span class="search-shortcut">/</span>
            </div>

            <select id="profileSelect" class="profile-selector" onchange="changeProfile(this.value)">
                <option value="maria_clara">❤️ Maria Clara</option>
                <option value="convidado">👤 Convidado</option>
                <option value="kids">🎈 Kids</option>
            </select>
        </div>
    </header>

    <main>
        <!-- HERO BANNER -->
        <div id="heroSection" class="hero-banner" style="display: none;">
            <div id="heroBg" class="hero-bg"></div>
            <div class="hero-overlay"></div>
            <div class="hero-content">
                <span class="hero-badge">🔥 Em Destaque Hoje</span>
                <h1 id="heroTitle" class="hero-title">Carregando Destaque...</h1>
                <p id="heroDesc" class="hero-desc">Descubra as melhores produções com alta performance e qualidade de exibição em um único lugar.</p>
                <div class="hero-btns">
                    <button id="heroPlayBtn" class="btn-action btn-primary" onclick="">▶ Assistir Agora</button>
                    <button id="heroDetailsBtn" class="btn-action" onclick="">ℹ️ Detalhes</button>
                </div>
            </div>
        </div>

        <!-- NAVEGAÇÃO DE TABS -->
        <div class="nav-tabs">
            <button class="tab-btn active" onclick="switchCategory('todos', this)">🔥 Lançamentos</button>
            <button class="tab-btn" onclick="switchCategory('filmes', this)">🎬 Filmes</button>
            <button class="tab-btn" onclick="switchCategory('series', this)">📺 Séries</button>
            <button class="tab-btn" onclick="switchCategory('animes', this)">🎌 Animes</button>
            <button class="tab-btn" onclick="switchCategory('favoritos', this)">❤️ Minha Lista</button>
        </div>

        <!-- CONTINUAR ASSISTINDO -->
        <div id="continueSection" style="display: none; margin-bottom: 28px;">
            <div class="section-header">
                <div class="section-title">Continuar Assistindo</div>
            </div>
            <div class="grid" id="continueGrid"></div>
        </div>

        <!-- CATÁLOGO PRINCIPAL -->
        <div class="section-header">
            <div class="section-title" id="catalogTitle">Últimos Lançamentos</div>
        </div>
        <div class="grid" id="catalogGrid"></div>

        <div style="text-align: center; margin-top: 24px;">
            <button id="loadMoreBtn" class="btn-action" onclick="loadMorePage()" style="display: none;">Carregar Mais Conteúdos</button>
        </div>
    </main>

    <!-- MODAL DE DETALHES & PLAYER CUSTOMIZADO -->
    <div class="modal" id="detailsModal" onclick="handleBackdropClick(event)">
        <div class="modal-card">
            <div class="modal-header">
                <div id="modalHeaderTitle" style="font-weight: 800; font-size: 1rem; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">Detalhes do Título</div>
                <button class="btn-action" onclick="closeModal()" style="padding: 6px 12px;">✕ Fechar</button>
            </div>
            <div class="modal-body" id="modalBody">
                <!-- Injetado via JS -->
            </div>
        </div>
    </div>

    <!-- CONTAINER DE TOASTS -->
    <div id="toastContainer" class="toast-container"></div>

    <footer>
        <p>CineFlix &copy; 2026 — Plataforma de Streaming de Alta Performance</p>
        <p style="margin-top: 6px;">✨ <strong>Feito com carinho para Maria Clara ❤️</strong> ✨</p>
    </footer>

    <script>
        let currentProfile = localStorage.getItem('current_profile') || 'maria_clara';
        let currentCategory = 'todos';
        let currentPage = 1;
        let activeMedia = null;
        let activeSearchAbort = null;
        let allFavoritesCache = [];
        const renderedHrefs = new Set();

        document.getElementById('profileSelect').value = currentProfile;

        // Atalho rápido '/' para pesquisa
        window.addEventListener('keydown', (e) => {
            if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
                e.preventDefault();
                document.getElementById('searchInput').focus();
            }
        });

        function showToast(message, icon = '✨') {
            const container = document.getElementById('toastContainer');
            const toast = document.createElement('div');
            toast.className = 'toast';
            toast.innerHTML = `<span>${icon}</span> <span>${message}</span>`;
            container.appendChild(toast);
            setTimeout(() => {
                toast.style.opacity = '0';
                toast.style.transform = 'translateX(100%)';
                toast.style.transition = 'all 0.3s ease';
                setTimeout(() => toast.remove(), 300);
            }, 2500);
        }

        function changeProfile(profileId) {
            currentProfile = profileId;
            localStorage.setItem('current_profile', profileId);
            showToast(`Perfil alterado!`, '👤');
            loadContinueWatching();
            if (currentCategory === 'favoritos') loadFavorites();
        }

        // ── 1. Hero Banner ──
        async function loadHeroBanner() {
            try {
                const res = await fetch('/api/featured');
                const items = await res.json();
                if (items && items.length > 0) {
                    const hero = items[0];
                    document.getElementById('heroSection').style.display = 'flex';
                    document.getElementById('heroBg').style.backgroundImage = `url('${hero.poster}')`;
                    document.getElementById('heroTitle').textContent = hero.title;
                    document.getElementById('heroPlayBtn').onclick = () => openDetails(hero.href);
                    document.getElementById('heroDetailsBtn').onclick = () => openDetails(hero.href);
                }
            } catch(e){}
        }

        // ── 2. Continuar Assistindo ──
        async function loadContinueWatching() {
            try {
                const res = await fetch(`/api/continue-watching?profile_id=${currentProfile}`);
                const list = await res.json();
                const container = document.getElementById('continueGrid');
                const section = document.getElementById('continueSection');

                if (!list || list.length === 0) {
                    section.style.display = 'none';
                    return;
                }

                section.style.display = 'block';
                container.innerHTML = list.map(item => `
                    <div class="card" onclick="openDetails('${item.href}')">
                        <div class="poster-wrap">
                            <span class="card-badge">${(item.type !== 'movie') ? `T${item.season} E${item.episode}` : 'FILME'}</span>
                            <img class="poster-img" src="${item.poster}" alt="${item.title}" loading="lazy" onerror="this.src='https://via.placeholder.com/300x450/141b2d/94a3b8?text=Sem+Capa'">
                            <div class="play-overlay">
                                <div class="play-icon-btn">▶</div>
                            </div>
                        </div>
                        <div class="card-info">
                            <div class="card-title">${item.title}</div>
                            <div class="progress-container">
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: ${item.progress_percent}%"></div>
                                </div>
                            </div>
                        </div>
                    </div>
                `).join('');
            } catch(e){}
        }

        // ── 3. Catálogo e Filtros ──
        async function loadCatalog(cat = 'todos', append = false) {
            const grid = document.getElementById('catalogGrid');
            if (!append) {
                renderedHrefs.clear();
                grid.innerHTML = Array(12).fill('<div class="card skeleton" style="height: 240px;"></div>').join('');
            }

            try {
                const res = await fetch(`/api/catalog?cat=${cat}&page=${currentPage}`);
                const data = await res.json();
                
                if (!append) grid.innerHTML = '';
                renderCards(data.items, grid, append);
                
                document.getElementById('loadMoreBtn').style.display = data.has_next ? 'inline-flex' : 'none';
            } catch(e) {
                if (!append) grid.innerHTML = '<p style="grid-column:1/-1; text-align:center; color:#ec4899; padding: 20px;">Fonte indisponível temporariamente.</p>';
            }
        }

        // ── 4. Busca com Debounce de 300ms ──
        let searchTimeout = null;
        document.getElementById('searchInput').addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            const query = e.target.value.trim();
            
            if (!query) {
                if (currentCategory === 'favoritos') {
                    renderCards(allFavoritesCache, document.getElementById('catalogGrid'), false);
                } else {
                    currentPage = 1;
                    loadCatalog(currentCategory);
                }
                return;
            }

            if (currentCategory === 'favoritos') {
                const filtered = allFavoritesCache.filter(item => item.title.toLowerCase().includes(query.toLowerCase()));
                const grid = document.getElementById('catalogGrid');
                renderedHrefs.clear();
                grid.innerHTML = '';
                renderCards(filtered, grid);
                return;
            }

            searchTimeout = setTimeout(async () => {
                if (activeSearchAbort) activeSearchAbort.abort();
                activeSearchAbort = new AbortController();

                const grid = document.getElementById('catalogGrid');
                renderedHrefs.clear();
                grid.innerHTML = Array(6).fill('<div class="card skeleton" style="height: 240px;"></div>').join('');
                
                try {
                    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`, { signal: activeSearchAbort.signal });
                    const data = await res.json();
                    grid.innerHTML = '';
                    renderCards(data.items, grid);
                    document.getElementById('loadMoreBtn').style.display = 'none';
                } catch(err) {
                    if (err.name !== 'AbortError') {
                        grid.innerHTML = '<p style="grid-column:1/-1; text-align:center; color:#ec4899; padding:20px;">Erro ao buscar resultados.</p>';
                    }
                }
            }, 300);
        });

        function renderCards(items, container, append = false) {
            if (!items || items.length === 0) {
                if (!append) container.innerHTML = '<p style="grid-column:1/-1; text-align:center; color:#94a3b8; padding: 30px;">Nenhum item encontrado.</p>';
                return;
            }

            const uniqueItems = items.filter(it => {
                if (!it.href || renderedHrefs.has(it.href)) return false;
                renderedHrefs.add(it.href);
                return true;
            });

            if (uniqueItems.length === 0 && !append) {
                container.innerHTML = '<p style="grid-column:1/-1; text-align:center; color:#94a3b8; padding: 30px;">Nenhum item novo.</p>';
                return;
            }

            const html = uniqueItems.map(it => `
                <div class="card" onclick="openDetails('${it.href}')">
                    <div class="poster-wrap">
                        <span class="card-badge">${(it.type || 'movie').substring(0, 3).toUpperCase()}</span>
                        <img class="poster-img" src="${it.poster}" alt="${it.title}" loading="lazy" onerror="this.src='https://via.placeholder.com/300x450/141b2d/94a3b8?text=Sem+Capa'">
                        <div class="play-overlay">
                            <div class="play-icon-btn">▶</div>
                        </div>
                    </div>
                    <div class="card-info">
                        <div class="card-title">${it.title}</div>
                    </div>
                </div>
            `).join('');

            if (append) container.innerHTML += html;
            else container.innerHTML = html;
        }

        function switchCategory(cat, btn) {
            currentCategory = cat;
            currentPage = 1;
            renderedHrefs.clear();
            document.getElementById('searchInput').value = '';
            
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            if (cat === 'favoritos') {
                document.getElementById('catalogTitle').textContent = 'Minha Lista de Favoritos';
                loadFavorites();
                document.getElementById('loadMoreBtn').style.display = 'none';
            } else {
                document.getElementById('catalogTitle').textContent = cat === 'todos' ? 'Últimos Lançamentos' : `Catálogo: ${cat.toUpperCase()}`;
                loadCatalog(cat);
            }
        }

        function loadMorePage() {
            currentPage++;
            loadCatalog(currentCategory, true);
        }

        async function loadFavorites() {
            const grid = document.getElementById('catalogGrid');
            renderedHrefs.clear();
            grid.innerHTML = Array(6).fill('<div class="card skeleton" style="height: 240px;"></div>').join('');
            try {
                const res = await fetch(`/api/favorites?profile_id=${currentProfile}`);
                allFavoritesCache = await res.json();
                grid.innerHTML = '';
                renderCards(allFavoritesCache, grid);
            } catch(e){}
        }

        // ── 5. Modal & Player Customizado Completo ──
        async function openDetails(href) {
            const modal = document.getElementById('detailsModal');
            const body = document.getElementById('modalBody');
            modal.classList.add('open');
            document.body.style.overflow = 'hidden';
            
            body.innerHTML = '<p style="text-align:center; padding:40px; color:#94a3b8;">Carregando detalhes do título...</p>';

            try {
                const [dRes, pRes, fRes] = await Promise.all([
                    fetch(`/api/details?href=${encodeURIComponent(href)}`),
                    fetch(`/api/progress/${encodeURIComponent(href)}?profile_id=${currentProfile}`),
                    fetch(`/api/favorites?profile_id=${currentProfile}`)
                ]);

                const details = await dRes.json();
                const progress = await pRes.json();
                const favs = await fRes.json();

                const isFavorite = favs.some(f => f.href === href);

                activeMedia = { href, details, progress, isFavorite };
                renderModalContent();
            } catch(e) {
                body.innerHTML = '<p style="text-align:center; color:#ec4899; padding:40px;">Erro ao carregar detalhes.</p>';
            }
        }

        function renderModalContent() {
            const { details, progress, isFavorite } = activeMedia;
            const body = document.getElementById('modalBody');
            document.getElementById('modalHeaderTitle').textContent = details.title || 'Assistir Conteúdo';

            const isMovie = details.type === 'movie';
            const hasEpisodes = details.seasons && details.seasons.some(s => s.episodes && s.episodes.length > 0);
            
            let currentS = progress.season || 1;
            let currentE = progress.episode || 1;
            let currentServer = progress.server || 'servidor-1';

            let playerUrl = '';
            let selectedEpObj = null;

            if (isMovie) {
                const srv = details.movie_servers.find(s => s.server === currentServer) || details.movie_servers[0];
                playerUrl = srv ? (srv.src || `https://fembed.sx/e/${details.tmdb_id || ''}/${srv.server}`) : (details.tmdb_id ? `https://fembed.sx/e/${details.tmdb_id}/servidor-1` : '');
            } else if (hasEpisodes) {
                const seasonObj = details.seasons.find(s => s.season_number === currentS) || details.seasons[0];
                if (seasonObj) {
                    currentS = seasonObj.season_number;
                    selectedEpObj = seasonObj.episodes.find(e => e.number === currentE) || seasonObj.episodes[0];
                    if (selectedEpObj) {
                        currentE = selectedEpObj.number;
                        playerUrl = selectedEpObj.base_url ? `${selectedEpObj.base_url}/${currentServer}` : selectedEpObj.player_url;
                    }
                }
            }

            const isDirectVideo = playerUrl.match(/\\.(mp4|m3u8|webm)$/i);

            body.innerHTML = `
                <div class="details-header">
                    <img class="details-poster" src="${details.poster}" alt="${details.title}" onerror="this.src='https://via.placeholder.com/300x450/141b2d/94a3b8?text=Sem+Capa'">
                    <div class="details-info">
                        <div class="details-title">${details.title}</div>
                        <div class="details-meta">
                            <span class="hero-badge" style="box-shadow:none;">${details.type.toUpperCase()}</span>
                            ${details.runtime ? `<span>⏱️ ${details.runtime}</span>` : ''}
                            ${details.genres.length ? `<span>🎭 ${details.genres.slice(0, 3).join(', ')}</span>` : ''}
                        </div>
                        ${details.director.length ? `<div style="font-size:0.75rem; color:#94a3b8;"><strong>Direção:</strong> ${details.director.slice(0, 2).join(', ')}</div>` : ''}
                        ${details.cast.length ? `<div style="font-size:0.75rem; color:#94a3b8;"><strong>Elenco:</strong> ${details.cast.slice(0, 4).join(', ')}</div>` : ''}
                        
                        <div class="details-synopsis">${details.synopsis || 'Sinopse não cadastrada.'}</div>
                        
                        <div style="display:flex; gap:10px; margin-top:10px; flex-wrap:wrap;">
                            <button class="btn-action ${isFavorite ? 'btn-primary' : ''}" onclick="toggleFavorite()">
                                ${isFavorite ? '❤️ Favoritado' : '➕ Minha Lista'}
                            </button>
                            <button class="btn-action" onclick="removeHistory()" style="color:#f87171;">🗑️ Limpar Progresso</button>
                        </div>
                    </div>
                </div>

                <!-- PLAYER CUSTOMIZADO -->
                ${playerUrl ? `
                    <div id="playerContainer" class="player-container">
                        ${isDirectVideo ? `
                            <video id="customVideo" class="player-video-elem" src="${playerUrl}" crossorigin="anonymous" playsinline></video>
                        ` : `
                            <iframe id="mainIframe" class="player-iframe-elem" src="${playerUrl}" referrerpolicy="no-referrer-when-downgrade" allowfullscreen allow="autoplay; encrypted-media"></iframe>
                            <div id="clickShield" class="click-shield" onclick="dismissClickShield()">
                                <div class="click-shield-btn">▶ Clique para Iniciar Reprodução Sem Anúncios</div>
                            </div>
                        `}

                        <!-- OVERLAY DE CONTROLES CUSTOMIZADOS -->
                        <div id="playerOverlay" class="player-controls-overlay">
                            <div class="player-top-bar">
                                <div class="player-title-text">${details.title} ${!isMovie ? `(T${currentS} E${currentE})` : ''}</div>
                                <div style="display:flex; gap:8px;">
                                    <button class="ctrl-btn" title="Picture-in-Picture" onclick="togglePiP()">📺</button>
                                    <button class="ctrl-btn" title="Pular Abertura (+30s)" onclick="skipTime(30)">⏩ +30s</button>
                                </div>
                            </div>

                            <div class="player-bottom-bar">
                                <div class="player-seek-container" onclick="seekVideoClick(event)">
                                    <div class="player-seek-bg">
                                        <div id="playerSeekProgress" class="player-seek-progress">
                                            <div class="player-seek-handle"></div>
                                        </div>
                                    </div>
                                </div>

                                <div class="player-buttons-row">
                                    <div class="player-left-ctrls">
                                        <button id="playPauseBtn" class="ctrl-btn" onclick="togglePlayPause()" title="Play/Pause (Espaço)">▶</button>
                                        <button class="ctrl-btn" onclick="skipTime(-10)" title="Voltar 10s">↺10</button>
                                        <button class="ctrl-btn" onclick="skipTime(10)" title="Avançar 10s">↻10</button>
                                        
                                        <div class="volume-container">
                                            <button id="volumeBtn" class="ctrl-btn" onclick="toggleMute()" title="Mutar (M)">🔊</button>
                                            <input type="range" id="volumeSlider" class="volume-slider" min="0" max="1" step="0.05" value="1" oninput="changeVolume(this.value)">
                                        </div>

                                        <span id="timeDisplay" class="time-display">0:00 / 0:00</span>
                                    </div>

                                    <div class="player-right-ctrls">
                                        <select id="speedSelect" onchange="changeSpeed(this.value)" style="background:rgba(0,0,0,0.5); border:1px solid var(--border-color); color:#fff; font-size:0.75rem; padding:4px; border-radius:4px; outline:none; cursor:pointer;">
                                            <option value="0.5">0.5x</option>
                                            <option value="1.0" selected>1.0x (Normal)</option>
                                            <option value="1.25">1.25x</option>
                                            <option value="1.5">1.5x</option>
                                            <option value="2.0">2.0x</option>
                                        </select>
                                        <button class="ctrl-btn" onclick="toggleFullscreen()" title="Tela Cheia (F)">⛶</button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                ` : '<div style="padding:40px; text-align:center; background:var(--bg-card); border-radius:var(--radius-md); color:#ec4899; font-weight:700;">Vídeo indisponível para este servidor.</div>'}

                ${isMovie && details.movie_servers.length > 0 ? `
                    <div>
                        <div class="section-title">Opções de Servidor</div>
                        <div style="display:flex; gap:8px; flex-wrap: wrap;">
                            ${details.movie_servers.map(s => `
                                <button class="btn-action ${s.server === currentServer ? 'btn-primary' : ''}" onclick="changeServer('${s.server}')">🖥️ ${s.label || s.server}</button>
                            `).join('')}
                        </div>
                    </div>
                ` : ''}

                ${!isMovie && hasEpisodes ? `
                    <div>
                        <div class="section-title">Temporadas</div>
                        <div style="display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; overflow-x: auto;">
                            ${details.seasons.map(s => `
                                <button class="btn-action ${s.season_number === currentS ? 'btn-primary' : ''}" onclick="changeSeason(${s.season_number})">Temporada ${s.season_number}</button>
                            `).join('')}
                        </div>

                        <div class="section-title">Episódios (T${currentS})</div>

                        <div class="ep-grid">
                            ${(details.seasons.find(s => s.season_number === currentS)?.episodes || []).map(e => `
                                <button class="ep-btn ${e.number === currentE ? 'active' : ''}" onclick="changeEpisode(${currentS}, ${e.number})">
                                    <span>Ep ${e.number}</span>
                                    <span class="lang-badge">${e.has_dub ? 'DUBLADO' : 'LEGENDADO'}</span>
                                </button>
                            `).join('')}
                        </div>

                        <div style="display:flex; gap:10px; margin-top:14px;">
                            <button class="btn-action" onclick="navigateEpisode(-1)" style="flex: 1;">⏮ Episódio Anterior</button>
                            <button class="btn-action btn-primary" onclick="navigateEpisode(1)" style="flex: 1;">Próximo Episódio ⏭</button>
                        </div>
                    </div>
                ` : ''}
            `;

            initCustomPlayerListeners();
            saveProgressState(currentS, currentE, currentServer);
        }

        // ── 6. Lógica de Eventos e Atalhos do Player ──
        function initCustomPlayerListeners() {
            const video = document.getElementById('customVideo');
            if (video) {
                video.addEventListener('timeupdate', updatePlayerProgress);
                video.addEventListener('ended', () => {
                    document.getElementById('playPauseBtn').textContent = '▶';
                    showToast('Vídeo finalizado', '✅');
                });
            }
        }

        function dismissClickShield() {
            const shield = document.getElementById('clickShield');
            if (shield) {
                shield.style.display = 'none';
                showToast('Reprodução iniciada', '▶');
            }
        }

        function togglePlayPause() {
            const video = document.getElementById('customVideo');
            const btn = document.getElementById('playPauseBtn');
            if (video) {
                if (video.paused) {
                    video.play();
                    if (btn) btn.textContent = '⏸';
                } else {
                    video.pause();
                    if (btn) btn.textContent = '▶';
                }
            } else {
                dismissClickShield();
            }
        }

        function skipTime(seconds) {
            const video = document.getElementById('customVideo');
            if (video) {
                video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + seconds));
                showToast(`${seconds > 0 ? '+' : ''}${seconds}s`, '⏩');
            } else {
                showToast(`Pulsou ${seconds}s no reprodutor`, '⚡');
            }
        }

        function toggleMute() {
            const video = document.getElementById('customVideo');
            const btn = document.getElementById('volumeBtn');
            if (video) {
                video.muted = !video.muted;
                if (btn) btn.textContent = video.muted ? '🔇' : '🔊';
            }
        }

        function changeVolume(val) {
            const video = document.getElementById('customVideo');
            if (video) {
                video.volume = val;
                video.muted = (val == 0);
            }
        }

        function changeSpeed(rate) {
            const video = document.getElementById('customVideo');
            if (video) {
                video.playbackRate = parseFloat(rate);
                showToast(`Velocidade: ${rate}x`, '⚡');
            }
        }

        function updatePlayerProgress() {
            const video = document.getElementById('customVideo');
            if (!video || isNaN(video.duration)) return;

            const pct = (video.currentTime / video.duration) * 100;
            document.getElementById('playerSeekProgress').style.width = `${pct}%`;

            const formatTime = (s) => {
                const mins = Math.floor(s / 60);
                const secs = Math.floor(s % 60);
                return `${mins}:${secs < 10 ? '0' : ''}${secs}`;
            };

            document.getElementById('timeDisplay').textContent = `${formatTime(video.currentTime)} / ${formatTime(video.duration)}`;
        }

        function seekVideoClick(e) {
            const video = document.getElementById('customVideo');
            if (!video || isNaN(video.duration)) return;

            const rect = e.currentTarget.getBoundingClientRect();
            const pos = (e.clientX - rect.left) / rect.width;
            video.currentTime = pos * video.duration;
        }

        async function togglePiP() {
            const video = document.getElementById('customVideo');
            if (video && document.pictureInPictureEnabled) {
                if (document.pictureInPictureElement) {
                    await document.exitPictureInPicture();
                } else {
                    await video.requestPictureInPicture();
                }
            } else {
                showToast('Picture-in-Picture não suportado para este player', '⚠️');
            }
        }

        function toggleFullscreen() {
            const container = document.getElementById('playerContainer');
            if (!container) return;

            if (!document.fullscreenElement) {
                if (container.requestFullscreen) container.requestFullscreen();
                else if (container.webkitRequestFullscreen) container.webkitRequestFullscreen();
            } else {
                if (document.exitFullscreen) document.exitFullscreen();
            }
        }

        // Atalhos globais de teclado para o Player (Espaço, F, M, Setas)
        window.addEventListener('keydown', (e) => {
            const modal = document.getElementById('detailsModal');
            if (!modal || !modal.classList.contains('open')) return;

            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            switch(e.code) {
                case 'Space':
                case 'KeyK':
                    e.preventDefault();
                    togglePlayPause();
                    break;
                case 'KeyF':
                    e.preventDefault();
                    toggleFullscreen();
                    break;
                case 'KeyM':
                    e.preventDefault();
                    toggleMute();
                    break;
                case 'ArrowLeft':
                    e.preventDefault();
                    skipTime(-5);
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    skipTime(5);
                    break;
                case 'KeyP':
                    e.preventDefault();
                    togglePiP();
                    break;
            }
        });

        function changeSeason(sNum) {
            activeMedia.progress.season = sNum;
            activeMedia.progress.episode = 1;
            renderModalContent();
        }

        function changeEpisode(sNum, eNum) {
            activeMedia.progress.season = sNum;
            activeMedia.progress.episode = eNum;
            renderModalContent();
        }

        function changeServer(serverName) {
            activeMedia.progress.server = serverName;
            renderModalContent();
        }

        function navigateEpisode(direction) {
            if (!activeMedia || !activeMedia.details.seasons) return;
            const seasons = activeMedia.details.seasons;
            
            let sIdx = seasons.findIndex(s => s.season_number === activeMedia.progress.season);
            if (sIdx === -1) sIdx = 0;

            const currentSeasonObj = seasons[sIdx];
            if (!currentSeasonObj) return;

            const epIdx = currentSeasonObj.episodes.findIndex(e => e.number === activeMedia.progress.episode);

            if (direction === 1) {
                if (epIdx < currentSeasonObj.episodes.length - 1) {
                    changeEpisode(currentSeasonObj.season_number, currentSeasonObj.episodes[epIdx + 1].number);
                } else if (sIdx < seasons.length - 1) {
                    const nextSeason = seasons[sIdx + 1];
                    if (nextSeason.episodes.length > 0) {
                        changeEpisode(nextSeason.season_number, nextSeason.episodes[0].number);
                    }
                }
            } else if (direction === -1) {
                if (epIdx > 0) {
                    changeEpisode(currentSeasonObj.season_number, currentSeasonObj.episodes[epIdx - 1].number);
                } else if (sIdx > 0) {
                    const prevSeason = seasons[sIdx - 1];
                    if (prevSeason.episodes.length > 0) {
                        const lastEp = prevSeason.episodes[prevSeason.episodes.length - 1];
                        changeEpisode(prevSeason.season_number, lastEp.number);
                    }
                }
            }
        }

        async function saveProgressState(season, ep, server) {
            if (!activeMedia) return;
            try {
                await fetch('/api/progress/save', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        profile_id: currentProfile,
                        item_id: activeMedia.href,
                        title: activeMedia.details.title,
                        poster_url: activeMedia.details.poster,
                        kind: activeMedia.details.type,
                        tmdb_id: activeMedia.details.tmdb_id,
                        imdb_id: activeMedia.details.imdb_id,
                        season: season,
                        episode: ep,
                        time_seconds: 60,
                        server: server
                    })
                });
                loadContinueWatching();
            } catch(e){}
        }

        async function toggleFavorite() {
            if (!activeMedia) return;
            try {
                const res = await fetch('/api/favorites/toggle', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        profile_id: currentProfile,
                        item_id: activeMedia.href,
                        title: activeMedia.details.title,
                        poster_url: activeMedia.details.poster,
                        kind: activeMedia.details.type,
                        href: activeMedia.href
                    })
                });
                const data = await res.json();
                activeMedia.isFavorite = data.is_favorite;
                showToast(data.is_favorite ? 'Adicionado à Minha Lista ❤️' : 'Removido da Minha Lista', data.is_favorite ? '❤️' : '🗑️');
                renderModalContent();
                if (currentCategory === 'favoritos') loadFavorites();
            } catch(e){}
        }

        async function removeHistory() {
            if (!activeMedia) return;
            if (confirm(`Remover "${activeMedia.details.title}" do histórico?`)) {
                try {
                    await fetch('/api/progress/delete', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ profile_id: currentProfile, item_id: activeMedia.href })
                    });
                    showToast('Histórico removido', '🗑️');
                    closeModal();
                    loadContinueWatching();
                } catch(e){}
            }
        }

        function closeModal() {
            document.getElementById('detailsModal').classList.remove('open');
            document.getElementById('modalBody').innerHTML = '';
            document.body.style.overflow = '';
            activeMedia = null;
        }

        function handleBackdropClick(e) {
            if (e.target.id === 'detailsModal') {
                closeModal();
            }
        }

        window.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && document.getElementById('detailsModal').classList.contains('open')) {
                closeModal();
            }
        });

        // Inicialização Principal
        loadHeroBanner();
        loadContinueWatching();
        loadCatalog('todos');
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
