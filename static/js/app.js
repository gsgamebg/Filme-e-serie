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
    if (!container) return;
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

// ── 4. Busca com Debounce ──
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
    if (btn) btn.classList.add('active');

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

// Inicialização Principal
loadHeroBanner();
loadContinueWatching();
loadCatalog('todos');
