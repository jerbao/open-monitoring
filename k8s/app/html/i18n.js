// status.jerb.net — i18n PT/EN
//
// Each element with `data-i18n="key"` has its content replaced in
// DOMContentLoaded. Manual switcher in the nav. Detects browser language,
// persists in localStorage.

(function () {
    'use strict';

    const dict = {
        pt: {
            'meta.title': 'status.jerb.net — open monitoring',
            'meta.desc': 'O que está rodando agora na minha infra. Atualizado direto do Prometheus sem mock.',

            // statusbar
            'sb.monitor': 'monitor',
            'sb.monitor.value': 'online',
            'sb.stack': 'stack',
            'sb.stack.value': 'prometheus + blackbox',
            'sb.updated': 'updated',

            // nav
            'nav.status': './status',
            'nav.portfolio': './portfolio',
            'nav.source': './source',

            // hero
            'hero.lede': 'O que está rodando agora na minha infra.',
            'hero.meta.refresh': 'Auto-refresh a cada',
            'hero.meta.probe': 'Probe ativo a cada',
            'hero.meta.retention': 'Retenção',

            // table
            'tbl.title': 'cat /api/status',
            'tbl.col.service': 'Serviço',
            'tbl.col.status': 'Status',
            'tbl.col.uptime24': 'Uptime 24h',
            'tbl.col.uptime7': '7d',
            'tbl.col.uptime30': '30d',
            'tbl.col.uptime90': '90d',
            'tbl.col.ssl': 'SSL',
            'tbl.loading': 'carregando…',

            // footer
            'footer.text': 'monitoring via prometheus + blackbox-exporter',
            'footer.made': 'made by',
            'footer.author': 'jerb',
            'footer.oss': 'open source',
        },

        en: {
            'meta.title': 'status.jerb.net — open monitoring',
            'meta.desc': 'What is running on my infra right now. Pulled straight from Prometheus, no mock.',

            // statusbar
            'sb.monitor': 'monitor',
            'sb.monitor.value': 'online',
            'sb.stack': 'stack',
            'sb.stack.value': 'prometheus + blackbox',
            'sb.updated': 'updated',

            // nav
            'nav.status': './status',
            'nav.portfolio': './portfolio',
            'nav.source': './source',

            // hero
            'hero.lede': 'What is running on my infra right now.',
            'hero.meta.refresh': 'Auto-refresh every',
            'hero.meta.probe': 'Active probe every',
            'hero.meta.retention': 'Retention',

            // table
            'tbl.title': 'cat /api/status',
            'tbl.col.service': 'Service',
            'tbl.col.status': 'Status',
            'tbl.col.uptime24': 'Uptime 24h',
            'tbl.col.uptime7': '7d',
            'tbl.col.uptime30': '30d',
            'tbl.col.uptime90': '90d',
            'tbl.col.ssl': 'SSL',
            'tbl.loading': 'loading…',

            // footer
            'footer.text': 'monitoring via prometheus + blackbox-exporter',
            'footer.made': 'made by',
            'footer.author': 'jerb',
            'footer.oss': 'open source',
        },
    };

    const STORAGE_KEY = 'status-lang';

    function detectLang() {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved && dict[saved]) return saved;
        const nav = (navigator.language || 'pt').toLowerCase();
        return nav.startsWith('pt') ? 'pt' : 'en';
    }

    function applyLang(lang) {
        const t = dict[lang] || dict.pt;
        localStorage.setItem(STORAGE_KEY, lang);
        document.documentElement.lang = lang === 'pt' ? 'pt-BR' : 'en';

        const title = t['meta.title'];
        if (title) document.title = title;
        const md = document.querySelector('meta[name="description"]');
        if (md && t['meta.desc']) md.setAttribute('content', t['meta.desc']);

        document.querySelectorAll('[data-i18n]').forEach((el) => {
            const k = el.getAttribute('data-i18n');
            const v = t[k];
            if (v === undefined) return;
            if (/[<>]/.test(v)) el.innerHTML = v;
            else el.textContent = v;
        });

        document.querySelectorAll('[data-i18n-attr]').forEach((el) => {
            const spec = el.getAttribute('data-i18n-attr');
            spec.split(';').forEach((pair) => {
                const [attr, key] = pair.split(':').map((s) => s.trim());
                if (attr && key && t[key] !== undefined) el.setAttribute(attr, t[key]);
            });
        });

        document.querySelectorAll('[data-lang]').forEach((b) => {
            b.classList.toggle('active', b.getAttribute('data-lang') === lang);
            b.setAttribute('aria-pressed', b.getAttribute('data-lang') === lang);
        });
    }

    document.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-lang]');
        if (!btn) return;
        applyLang(btn.getAttribute('data-lang'));
    });

    document.addEventListener('DOMContentLoaded', () => {
        applyLang(detectLang());
    });
})();
