/* static/js/article_detail_renderer.js */

window.ArticleDetailRenderer = {
    // 基础防 XSS 转义（正文需要渲染 HTML 时不使用这个）
    escapeHTML: function (str) {
        if (str === null || str === undefined) return '';
        return String(str).replace(/[&<>"']/g, function (m) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m];
        });
    },

    anyTimeToTimeStr: function (s) {
        try {
            const d = new Date(s);
            if (isNaN(d.getTime())) return s || 'N/A';
            const y = d.getFullYear();
            const m = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            const hh = String(d.getHours()).padStart(2, '0');
            const mm = String(d.getMinutes()).padStart(2, '0');
            const ss = String(d.getSeconds()).padStart(2, '0');
            return `${y}-${m}-${day} ${hh}:${mm}:${ss}`;
        } catch { return s || 'N/A'; }
    },

    // 仅允许 http/https 协议，防止 javascript: 等危险 scheme 注入
    isSafeUrl: function (url) {
        if (!url) return false;
        if (/^https?:\/\//i.test(url)) return true;
        if (url.startsWith('/') && !url.startsWith('//')) return true;
        return false;
    },

    // 白名单 HTML 消毒：仅保留安全排版标签与属性，剥离 script/iframe/事件属性
    sanitizeHtml: function (html) {
        if (!html) return '';
        const doc = new DOMParser().parseFromString(String(html), 'text/html');

        // 递归清理：删除危险节点与事件属性
        const ALLOWED_TAGS = new Set([
            'P', 'BR', 'B', 'STRONG', 'I', 'EM', 'U', 'A', 'UL', 'OL', 'LI',
            'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'CODE', 'PRE',
            'DIV', 'SPAN', 'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD', 'HR'
        ]);

        function cleanNode(node) {
            // 删除脚本、iframe、object、embed、link、meta、style 等危险/无关节点
            if (node.nodeType === Node.ELEMENT_NODE) {
                const tag = node.tagName;
                if (!ALLOWED_TAGS.has(tag) || /^(SCRIPT|IFRAME|OBJECT|EMBED|LINK|META|STYLE|FORM|INPUT|BUTTON)$/i.test(tag)) {
                    // 若为 img 等保留标签外的节点，仅保留其文本内容（去标签化）
                    while (node.firstChild) node.parentNode.insertBefore(node.firstChild, node);
                    node.remove();
                    return;
                }
                // 删除所有事件属性（on*）
                Array.from(node.attributes).forEach(attr => {
                    const name = attr.name.toLowerCase();
                    if (name.startsWith('on') || name === 'style' || name === 'srcdoc') {
                        node.removeAttribute(attr.name);
                    }
                    if (name === 'href' && !this.isSafeUrl(attr.value)) {
                        node.removeAttribute('href');
                    }
                });
            }
            Array.from(node.childNodes).forEach(child => cleanNode(child));
        }

        // 用调用方 this 调用递归，保证 isSafeUrl 可访问
        const self = this;
        function cleanNodeBound(node) { return cleanNode.call(self, node); }
        Array.from(doc.body.childNodes).forEach(cleanNodeBound);
        return doc.body.innerHTML;
    },

    renderStars: function (score) {
        const num = Number(score);
        if (!Number.isFinite(num)) return '';
        const full = Math.floor(num / 2);
        const half = (num % 2) >= 1;
        const empty = 5 - full - (half ? 1 : 0);
        let html = '<span class="stars">';
        for (let i = 0; i < full; i++) html += '<i class="bi bi-star-fill text-warning"></i> ';
        if (half) html += '<i class="bi bi-star-half text-warning"></i> ';
        for (let i = 0; i < empty; i++) html += '<i class="bi bi-star text-warning"></i> ';
        html += ` <span class="ms-2 text-muted">${num}/10</span></span>`;
        return html;
    },

    renderRatingTable: function (article) {
        const rates = article?.RATE || {};
        const manualRatings = (article?.APPENDIX && (article.APPENDIX['__MANUAL_RATING__'] || article.APPENDIX['APPENDIX_MANUAL_RATING'])) || {};
        const keys = Object.keys(rates);
        if (!keys.length) return '';

        let rows = '';
        keys.forEach((key, index) => {
            const score = rates[key];
            if (typeof score !== 'number' || score < 0 || score > 10) return;
            const manual = (manualRatings && manualRatings[key] != null) ? manualRatings[key] : score;
            const inputId = `rating-${index}`;
            rows += `
                <tr>
                    <td>${this.escapeHTML(key)}</td>
                    <td>${this.renderStars(score)}</td>
                    <td>
                        <label class="visually-hidden" for="${inputId}">${this.escapeHTML(key)} manual rating</label>
                        <input id="${inputId}" data-rating-dimension="${this.escapeHTML(key)}" type="number" class="form-control form-control-sm"
                            value="${this.escapeHTML(manual)}" min="0" max="10" step="0.5" style="width: 80px;"
                            oninput="if(this.value<0)this.value=0;if(this.value>10)this.value=10;">
                    </td>
                </tr>`;
        });

        return `
            <div class="mt-4">
                <h5><i class="bi bi-graph-up"></i> Analysis & Evaluation</h5>
                <div class="table-responsive">
                    <table class="table table-sm">
                        <thead><tr><th>Dimension</th><th>Rating</th><th>Manual Rating</th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
                <div class="mt-3">
                    <button id="submit-rating" class="btn btn-primary btn-sm">
                        <i class="bi bi-check-circle"></i> Submit Manual Ratings
                    </button>
                </div>
            </div>`;
    },

    // 统一生成 HTML 结构
    generateHTML: function (article) {
        const isAuthenticated = window.IS_AUTHENTICATED === true;
        const rawUuid = String(article?.UUID || '');
        const uuid = this.escapeHTML(rawUuid);
        const rawInformant = article?.INFORMANT || '';
        // 仅当来源 URL 通过了 http/https 白名单时才展示链接
        const informant = this.isSafeUrl(rawInformant) ? this.escapeHTML(rawInformant) : '';
        const pubTime = this.escapeHTML(this.anyTimeToTimeStr(article?.APPENDIX?.['__TIME_PUB__'] || article?.PUB_TIME || 'N/A')); // v2 优先取 APPENDIX.__TIME_PUB__，回退 v1 的 PUB_TIME
        const title = this.escapeHTML(article?.EVENT_TITLE || 'No Title');
        const brief = this.escapeHTML(article?.EVENT_BRIEF || 'No Brief');

        // 正文按白名单消毒后渲染：支持基础排版，同时剥离脚本、iframe 与事件属性
        const content = this.sanitizeHtml(article?.EVENT_TEXT || 'No Content');

        const locations = Array.isArray(article?.LOCATION) ? article.LOCATION.map(i => this.escapeHTML(i)) : [];
        const people = Array.isArray(article?.PEOPLE) ? article.PEOPLE.map(i => this.escapeHTML(i)) : [];
        const orgs = Array.isArray(article?.ORGANIZATION) ? article.ORGANIZATION.map(i => this.escapeHTML(i)) : [];
        const times = Array.isArray(article?.TIME)
            ? article.TIME.map(t => this.escapeHTML(this.anyTimeToTimeStr(t)))
            : [];
        const impact = this.escapeHTML(article?.IMPACT || 'No Impact');
        const tips = this.escapeHTML(article?.TIPS || 'No Tips');

        // [修复] 补全了 <a> 标签
        return `
        <div class="article-preview" id="article-root-${uuid}" data-uuid="${uuid}">
            <section class="header-box key-points">
                <div style="display:flex; flex-wrap:wrap; gap:8px; align-items:center; justify-content:space-between;">
                    <div style="min-width:0;">
                        <div style="font-size:12px; color:#555;">
                            <i class="bi bi-calendar-event"></i> ${pubTime}
                            &nbsp;|&nbsp; <i class="bi bi-upc-scan"></i> ${uuid}
                        </div>
                        <h3 style="margin:6px 0 0; font-size:20px; line-height:1.35;">${title}</h3>
                        <div style="margin-top:6px; color:#333;">${brief}</div>
                    </div>
                    <div style="display:flex; gap:8px; align-items:center;">
                        ${informant ? `<a href="${informant}" target="_blank" rel="noopener noreferrer" class="btn btn-outline-secondary btn-sm"><i class="bi bi-link-45deg"></i> Source</a>` : ''}
                        ${window.IS_AUTHENTICATED === true ? `
                        <a href="/intelligences?search_mode=vector_similar&reference=${encodeURIComponent(rawUuid)}&score_threshold=0.6" class="btn btn-outline-primary btn-sm">
                            <i class="bi bi-intersect"></i> Find Similar
                        </a>` : ''}
                    </div>
                </div>
            </section>

            ${isAuthenticated ? `
            <section class="meta-grid">
                <div class="meta-card">
                    <h5><i class="bi bi-geo-alt"></i> Geographic Locations</h5>
                    ${locations.length ? locations.join(', ') : 'No location data'}
                </div>
                <div class="meta-card">
                    <h5><i class="bi bi-people"></i> Related People</h5>
                    ${people.length ? people.join(', ') : 'No associated people'}
                </div>
                <div class="meta-card">
                    <h5><i class="bi bi-building"></i> Related Organizations</h5>
                    ${orgs.length ? orgs.join(', ') : 'No related organizations'}
                </div>
            </section>

            <section class="meta-card" style="margin-top:10px;">
                <h5><i class="bi bi-clock-history"></i> Event Time(s)</h5>
                ${times.length ? times.join(', ') : 'No specific timing data'}
            </section>

            <section class="content-section" style="margin-top:16px;">
                ${content}
            </section>

            <section style="margin-top:16px;">
                ${this.renderRatingTable(article)}
            </section>

            <section class="impact-card" style="margin-top:16px;">
                <h5><i class="bi bi-lightning-charge"></i> Potential Impact</h5>
                <p>${impact}</p>
            </section>

            <section class="tip-card" style="margin-top:12px;">
                <h5><i class="bi bi-lightbulb"></i> Analyst Notes</h5>
                <p>${tips}</p>
            </section>` : `
            <section class="meta-card guest-restricted-notice" style="margin-top:16px;">
                <h5><i class="bi bi-shield-lock"></i> 受限内容</h5>
                <p>公开模式仅显示摘要，<a href="/login">登录</a>后查看全文与分析。</p>
            </section>`}
        </div>`;
    },

    // 绑定评分提交逻辑
    bindEvents: function (containerElement, uuid, toastContainerId) {
        const submitBtn = containerElement.querySelector('#submit-rating');
        if (!submitBtn) return;

        submitBtn.addEventListener('click', async () => {
            const ratings = {};
            containerElement.querySelectorAll('input[data-rating-dimension]').forEach(input => {
                const dim = input.dataset.ratingDimension;
                ratings[dim] = input.value;
            });

            try {
                submitBtn.disabled = true;
                const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
                const r = await fetch('/manual_rate', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': csrfToken
                    },
                    body: JSON.stringify({
                        uuid: uuid,
                        ratings: ratings,
                        timestamp: new Date().toISOString()
                    })
                });
                if (!r.ok) {
                    const payload = await r.json().catch(() => ({}));
                    throw new Error(payload.message || `HTTP ${r.status}`);
                }
                this.showToast(toastContainerId, 'Ratings submitted successfully!', 'success');
            } catch (e) {
                this.showToast(toastContainerId, 'Error submitting ratings: ' + (e.message || e), 'danger');
            } finally {
                submitBtn.disabled = false;
            }
        });
    },

    showToast: function (containerId, message, type) {
        const container = document.getElementById(containerId);
        if (!container) return;
        const toast = document.createElement('div');
        toast.className = `article-toast ${type}`;
        // textContent 防止错误消息注入 HTML
        const msgDiv = document.createElement('div');
        msgDiv.style.flex = '1';
        msgDiv.textContent = message;
        const closeBtn = document.createElement('button');
        closeBtn.className = 'toast-close';
        closeBtn.type = 'button';
        closeBtn.setAttribute('aria-label', '关闭提示');
        closeBtn.innerHTML = '<i class="bi bi-x-lg"></i>';
        toast.appendChild(msgDiv);
        toast.appendChild(closeBtn);
        container.appendChild(toast);
        const remove = () => toast.remove();
        closeBtn.addEventListener('click', remove);
        setTimeout(remove, 2400);
    }
};
