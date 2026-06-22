// open-monitoring — fetch /api/status and renders table

(function () {
    'use strict';

    const REFRESH_MS = 60_000;
    const tbody = document.getElementById('status-body');
    const updatedEl = document.getElementById('updated');

    function fmtUptime(pct) {
        if (pct === null || pct === undefined) return { text: '—', cls: '' };
        const cls = pct >= 99.9 ? '' : pct >= 99.0 ? 'warn' : 'bad';
        return { text: pct.toFixed(2) + '%', cls };
    }

    function fmtSsl(days) {
        if (days === null || days === undefined) return { text: '—', cls: 'none' };
        if (days <= 0) return { text: 'expirado', cls: 'bad' };
        if (days <= 14) return { text: days + 'd', cls: 'bad' };
        if (days <= 30) return { text: days + 'd', cls: 'warn' };
        return { text: days + 'd', cls: '' };
    }

    function fmtRelative(iso) {
        const d = new Date(iso);
        const diff = Math.floor((Date.now() - d.getTime()) / 1000);
        if (diff < 60) return diff + 's atrás';
        if (diff < 3600) return Math.floor(diff / 60) + 'min atrás';
        if (diff < 86400) return Math.floor(diff / 3600) + 'h atrás';
        return Math.floor(diff / 86400) + 'd atrás';
    }

    function render(data) {
        if (!data.services || data.services.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--fg-dim);">nenhum probe ativo. verifique o prometheus.yml</td></tr>';
            return;
        }

        // Agrupa por group
        const groups = {};
        for (const svc of data.services) {
            (groups[svc.group] = groups[svc.group] || []).push(svc);
        }

        let html = '';
        for (const [groupName, svcs] of Object.entries(groups)) {
            html += `<tr class="group-header"><td colspan="7">// ${groupName}</td></tr>`;
            for (const s of svcs) {
                const u24 = fmtUptime(s.uptime['24h']);
                const u7  = fmtUptime(s.uptime['7d']);
                const u30 = fmtUptime(s.uptime['30d']);
                const u90 = fmtUptime(s.uptime['90d']);
                const ssl = fmtSsl(s.ssl_days);

                html += `<tr>
                    <td><strong>${s.name}</strong></td>
                    <td><span class="dot-status ${s.status}"></span>${s.status}</td>
                    <td class="uptime-cell ${u24.cls}"><span class="pct">${u24.text}</span></td>
                    <td class="uptime-cell ${u7.cls}"><span class="pct">${u7.text}</span></td>
                    <td class="uptime-cell ${u30.cls}"><span class="pct">${u30.text}</span></td>
                    <td class="uptime-cell ${u90.cls}"><span class="pct">${u90.text}</span></td>
                    <td class="ssl-cell ${ssl.cls}">${ssl.text}</td>
                </tr>`;
            }
        }
        tbody.innerHTML = html;
        updatedEl.textContent = fmtRelative(data.updated_at);
        updatedEl.title = data.updated_at;
    }

    async function fetchStatus() {
        try {
            const r = await fetch('/api/status', { cache: 'no-store' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const data = await r.json();
            render(data);
        } catch (e) {
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--red);">erro: ${e.message}</td></tr>`;
        }
    }

    fetchStatus();
    setInterval(fetchStatus, REFRESH_MS);
})();
