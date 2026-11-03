// Settings > Connections > Webhook secret: Show, Copy and Regenerate. The secret is fetched when asked for (POST, with the
// page's CSRF token that csrf_fetch.js adds) and is never in the page source; Copy puts it on the clipboard without
// showing it.
(function () {
    'use strict';

    var panel = document.getElementById('webhookSecretPanel');
    if (!panel) return;

    var field = document.getElementById('webhookSecretValue');
    var showButton = document.getElementById('webhookSecretShow');
    var copyButton = document.getElementById('webhookSecretCopy');
    var regenerateButton = document.getElementById('webhookSecretRegenerate');
    var status = document.getElementById('webhookSecretStatus');
    var masked = panel.getAttribute('data-masked');

    function say(text, ok) {
        status.className = 'small mt-2 mb-0 ' + (ok ? 'text-success' : 'text-danger');
        status.textContent = text;
    }

    function post(url) {
        return fetch(url, { method: 'POST', credentials: 'same-origin', cache: 'no-store' }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
                return data;
            });
        });
    }

    function setShown(secret) {
        var shown = Boolean(secret);
        field.value = shown ? secret : masked;
        showButton.textContent = shown ? 'Hide' : 'Show';
        showButton.setAttribute('aria-pressed', shown ? 'true' : 'false');
    }

    function writeToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
        // Plain-HTTP LAN pages have no async clipboard: select a temporary field and copy it the old way.
        return new Promise(function (resolve, reject) {
            var temp = document.createElement('textarea');
            temp.value = text;
            temp.setAttribute('readonly', '');
            temp.style.position = 'fixed';
            temp.style.opacity = '0';
            document.body.appendChild(temp);
            temp.select();
            var copied = document.execCommand && document.execCommand('copy');
            temp.remove();
            copied ? resolve() : reject(new Error('copy blocked'));
        });
    }

    showButton.addEventListener('click', function () {
        if (showButton.getAttribute('aria-pressed') === 'true') {
            setShown(null);
            return;
        }
        post(panel.getAttribute('data-reveal-url')).then(function (data) {
            setShown(data.secret);
            say('', true);
        }).catch(function (error) { say('Could not show the secret: ' + error.message, false); });
    });

    copyButton.addEventListener('click', function () {
        post(panel.getAttribute('data-reveal-url')).then(function (data) {
            return writeToClipboard(data.secret);
        }).then(function () {
            say('Copied to the clipboard.', true);
        }).catch(function () {
            say('Could not copy it from here. Use Show, then select the text and copy it.', false);
        });
    });

    regenerateButton.addEventListener('click', function () {
        var message = 'Make a new webhook secret? Sonarr, Radarr, Emby, Jellyfin and any script using the old one stop working until you paste the new one in. Plex is updated for you.';
        var ask = typeof window.appConfirm === 'function'
            ? window.appConfirm(message, { title: 'Regenerate webhook secret', confirmText: 'Regenerate' })
            : Promise.resolve(window.confirm(message));
        Promise.resolve(ask).then(function (confirmed) {
            if (!confirmed) return null;
            return post(panel.getAttribute('data-regenerate-url')).then(function (data) {
                masked = data.masked;
                panel.setAttribute('data-masked', masked);
                setShown(data.secret);
                say('New secret made and shown above. Copy it into Sonarr, Radarr and any scripts. Plex was updated.', true);
            });
        }).catch(function (error) { say('Could not make a new secret: ' + error.message, false); });
    });
})();
