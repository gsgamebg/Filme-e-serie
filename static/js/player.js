let hlsInstance = null;

async function openDetails(href) {
    const modal = document.getElementById('detailsModal');
    const body = document.getElementById('modalBody');
    modal.classList.add('open');
    document.body.style.overflow = 'hidden';
    
    body.innerHTML = '<p style="text-align:center; padding:40px; color:#94a3b8;">Carregando detalhes e resolvendo servidor...</p>';

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
        await renderModalContent();
    } catch(e) {
        body.innerHTML = '<p style="text-align:center; color:#ec4899; padding:40px;">Erro ao carregar detalhes.</p>';
    }
}

async function renderModalContent() {
    const { details, progress, isFavorite } = activeMedia;
    const body = document.getElementById('modalBody');
    document.getElementById('modalHeaderTitle').textContent = details.title || 'Assistir Conteúdo';

    const isMovie = details.type === 'movie';
    const hasEpisodes = details.seasons && details.seasons.some(s => s.episodes && s.episodes.length > 0);
    
    let currentS = progress.season || 1;
    let currentE = progress.episode || 1;
    let currentServer = progress.server || 'servidor-1';

    let rawPlayerUrl = '';
    let selectedEpObj = null;

    if (isMovie) {
        const srv = details.movie_servers.find(s => s.server === currentServer) || details.movie_servers[0];
        rawPlayerUrl = srv ? (srv.src || `https://fembed.sx/e/${details.tmdb_id || ''}/${srv.server}`) : (details.tmdb_id ? `https://fembed.sx/e/${details.tmdb_id}/servidor-1` : '');
    } else if (hasEpisodes) {
        const seasonObj = details.seasons.find(s => s.season_number === currentS) || details.seasons[0];
        if (seasonObj) {
            currentS = seasonObj.season_number;
            selectedEpObj = seasonObj.episodes.find(e => e.number === currentE) || seasonObj.episodes[0];
            if (selectedEpObj) {
                currentE = selectedEpObj.number;
                rawPlayerUrl = selectedEpObj.base_url ? `${selectedEpObj.base_url}/${currentServer}` : selectedEpObj.player_url;
            }
        }
    }

    // Tenta resolver o stream direto (.m3u8 ou .mp4) no backend
    let streamData = { direct_url: null, type: 'iframe' };
    if (rawPlayerUrl) {
        try {
            const streamRes = await fetch(`/api/stream?url=${encodeURIComponent(rawPlayerUrl)}`);
            streamData = await streamRes.json();
        } catch(e){}
    }

    const streamUrl = streamData.direct_url || rawPlayerUrl;
    const isDirect = Boolean(streamData.direct_url || streamUrl.match(/\.(mp4|m3u8|webm)$/i));

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
        ${streamUrl ? `
            <div id="playerContainer" class="player-container">
                ${isDirect ? `
                    <video id="customVideo" class="player-video-elem" crossorigin="anonymous" playsinline controlslist="nodownload"></video>
                ` : `
                    <iframe id="mainIframe" class="player-iframe-elem" src="${streamUrl}" referrerpolicy="no-referrer-when-downgrade" allowfullscreen allow="autoplay; encrypted-media"></iframe>
                    <div id="clickShield" class="click-shield" onclick="dismissClickShield()">
                        <div class="click-shield-btn">▶ Iniciar Reprodução Sem Anúncios</div>
                    </div>
                `}

                <!-- OVERLAY DE CONTROLES CUSTOMIZADOS -->
                <div id="playerOverlay" class="player-controls-overlay">
                    <div class="player-top-bar">
                        <div class="player-title-text">${details.title} ${!isMovie ? `(T${currentS} E${currentE})` : ''} ${streamData.direct_url ? '⚡ Stream Direto' : ''}</div>
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

    if (isDirect && streamUrl) {
        initHlsOrDirectVideo(streamUrl, streamData.type);
    }
    saveProgressState(currentS, currentE, currentServer);
}

function initHlsOrDirectVideo(url, type) {
    const video = document.getElementById('customVideo');
    if (!video) return;

    if (hlsInstance) {
        hlsInstance.destroy();
        hlsInstance = null;
    }

    const isHls = type === 'hls' || url.includes('.m3u8');

    if (isHls && Hls.isSupported()) {
        hlsInstance = new Hls({
            enableWorker: true,
            lowLatencyMode: true
        });
        hlsInstance.loadSource(url);
        hlsInstance.attachMedia(video);
        hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
            showToast('Stream HLS carregado com alta performance ⚡', '🚀');
        });
    } else if (video.canPlayType('application/vnd.apple.mpegurl') && isHls) {
        video.src = url;
    } else {
        video.src = url;
    }

    video.addEventListener('timeupdate', updatePlayerProgress);
    video.addEventListener('ended', () => {
        const btn = document.getElementById('playPauseBtn');
        if (btn) btn.textContent = '▶';
        showToast('Vídeo finalizado', '✅');
    });
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
        video.currentTime = Math.max(0, Math.min(video.duration || 0, video.currentTime + seconds));
        showToast(`${seconds > 0 ? '+' : ''}${seconds}s`, '⏩');
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
    const bar = document.getElementById('playerSeekProgress');
    if (bar) bar.style.width = `${pct}%`;

    const formatTime = (s) => {
        const mins = Math.floor(s / 60);
        const secs = Math.floor(s % 60);
        return `${mins}:${secs < 10 ? '0' : ''}${secs}`;
    };

    const disp = document.getElementById('timeDisplay');
    if (disp) disp.textContent = `${formatTime(video.currentTime)} / ${formatTime(video.duration)}`;
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
        showToast('Picture-in-Picture não disponível', '⚠️');
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
    const modal = document.getElementById('detailsModal');
    modal.classList.remove('open');
    document.getElementById('modalBody').innerHTML = '';
    document.body.style.overflow = '';
    if (hlsInstance) {
        hlsInstance.destroy();
        hlsInstance = null;
    }
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
