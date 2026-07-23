import sqlite3
import requests
import json
import re
import time
import logging
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, Response
from urllib.parse import quote, urljoin, urlparse

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

app = Flask(__name__)

# ── 1. CONFIGURAÇÕES & CONSTANTES ─────────────────────────────────────────
DB_NAME = "historico.db"
UPSTREAM_BASE = "https://www.mendigoflix.lol"
ALLOWED_HOSTS = {'www.mendigoflix.lol', 'mendigoflix.lol', 'redecanais.es', 'fembed.sx', 'embed.lol'}
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

# ── 4. EXTRATOR & RESOLVER DE STREAM DIRETO (.m3u8 / .mp4) ────────────────
def unpack_js(packed_code):
    try:
        match = re.search(r"eval\(function\(p,a,c,k,e,d\)\{.*\}\('([^']*)',(\d+),(\d+),'([^']*)'\.split\('\|'\)", packed_code)
        if not match:
            return ""
        p, a, c, k = match.groups()
        a, c = int(a), int(c)
        k_list = k.split('|')
        def baseN(num, b):
            return ((num == 0) and "0") or (baseN(num // b, b).lstrip("0") + "0123456789abcdefghijklmnopqrstuvwxyz"[num % b])
        for idx, val in enumerate(k_list):
            if val:
                key_str = baseN(idx, a)
                p = re.sub(r'\b' + key_str + r'\b', val, p)
        return p
    except Exception:
        return ""

def extract_direct_stream(embed_url):
    if not embed_url:
        return None, None

    # Se a URL já for um link direto de vídeo
    if re.search(r'\.(m3u8|mp4|webm)($|\?)', embed_url, re.IGNORECASE):
        stream_type = 'hls' if '.m3u8' in embed_url.lower() else 'mp4'
        return embed_url, stream_type

    cache_key = f"stream_resolve_{embed_url}"
    cached = get_cached_data(cache_key, ttl_seconds=1800)
    if cached and isinstance(cached, dict):
        return cached.get('direct_url'), cached.get('type')

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Referer': UPSTREAM_BASE
        }
        res = HTTP.get(embed_url, headers=headers, timeout=8)
        html = res.text

        # Tenta descompactar JS se houver script empacotado
        if 'eval(function(p,a,c,k,e,d)' in html:
            unpacked = unpack_js(html)
            if unpacked:
                html += "\n" + unpacked

        # 1. Busca por URLs .m3u8
        m3u8_match = re.search(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html, re.IGNORECASE)
        if m3u8_match:
            stream_url = m3u8_match.group(1).replace('\\/', '/')
            set_cached_data(cache_key, {'direct_url': stream_url, 'type': 'hls'})
            return stream_url, 'hls'

        # 2. Busca por URLs .mp4
        mp4_match = re.search(r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)', html, re.IGNORECASE)
        if mp4_match:
            stream_url = mp4_match.group(1).replace('\\/', '/')
            set_cached_data(cache_key, {'direct_url': stream_url, 'type': 'mp4'})
            return stream_url, 'mp4'

        # 3. Busca por definições de fontes em JWPlayer / Clappr (file: "...")
        file_match = re.search(r'(?:file|source|src)\s*:\s*["\'](https?://[^"\']+)["\']', html, re.IGNORECASE)
        if file_match:
            stream_url = file_match.group(1).replace('\\/', '/')
            stream_type = 'hls' if '.m3u8' in stream_url.lower() else 'mp4'
            set_cached_data(cache_key, {'direct_url': stream_url, 'type': stream_type})
            return stream_url, stream_type

        # 4. Busca em elementos HTML <video> ou <source>
        soup = BeautifulSoup(html, 'html.parser')
        video_source = soup.select_one('video source[src], video[src]')
        if video_source:
            src = video_source.get('src')
            if src:
                stream_url = urljoin(embed_url, src)
                stream_type = 'hls' if '.m3u8' in stream_url.lower() else 'mp4'
                set_cached_data(cache_key, {'direct_url': stream_url, 'type': stream_type})
                return stream_url, stream_type

        # 5. Busca por iframes internos secundários
        sec_iframe = soup.select_one('iframe[src]')
        if sec_iframe:
            sec_src = sec_iframe.get('src')
            if sec_src and sec_src != embed_url and not sec_src.startswith('javascript:'):
                sec_url = urljoin(embed_url, sec_src)
                return extract_direct_stream(sec_url)

    except Exception as e:
        logging.error(f"Erro ao extrair stream de {embed_url}: {e}")

    return None, None

def safe_http_get(url):
    res = HTTP.get(url, timeout=(3.05, 10))
    res.raise_for_status()
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
    cached = get_cached_data(cache_key, ttl_seconds=1800)
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

        # 2. Episódios
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

        # 3. Servidores de Filme & Alternativas Limpas
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
                    'label': 'Principal (RedeCanais)',
                    'src': iframe_src
                })

        if details.get('tmdb_id'):
            tmdb = details['tmdb_id']
            principal_src = servers[0]['src'] if servers else f"https://fembed.sx/e/{tmdb}/servidor-1"
            details['movie_servers'] = [
                {'server': 'servidor-1', 'label': 'Principal (Servidor 1)', 'src': principal_src},
                {'server': 'servidor-2', 'label': 'Embed.lol (Sem Ads ⚡)', 'src': f"https://embed.lol/embed/movie/{tmdb}"},
                {'server': 'servidor-3', 'label': 'VidSrc (HD Fast 🚀)', 'src': f"https://vidsrc.to/embed/movie/{tmdb}"},
                {'server': 'servidor-4', 'label': 'SuperFlix (Opção 4 🎬)', 'src': f"https://superflixapi.pro/filme/{tmdb}"}
            ]
        else:
            details['movie_servers'] = servers

        set_cached_data(cache_key, details)
    except Exception as e:
        logging.error(f"Erro ao buscar detalhes ({href}): {e}")

    return details

# ── 5. ROTAS DA API REST ──────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

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

@app.route('/api/stream')
def api_stream():
    url = request.args.get('url', '')
    if not url:
        return jsonify({'direct_url': None, 'type': 'iframe'}), 400
    
    direct_url, stream_type = extract_direct_stream(url)
    return jsonify({
        'direct_url': direct_url,
        'type': stream_type or 'iframe'
    })

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
