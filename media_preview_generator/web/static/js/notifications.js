/* Recent activity shows server-backed notices. Job history and saved Plex sends have their own pages. */
var notificationLoad = null;
var notificationMutation = false;
var notificationSnapshot = null;

function loadNotifications() {
    var list = document.getElementById('notificationList');
    if (!list || notificationLoad) return notificationLoad;

    var loading = document.getElementById('notificationLoading');
    var failure = document.getElementById('notificationFetchError');
    var trigger = document.getElementById('notificationBellBtn');
    list.setAttribute('aria-busy', 'true');
    notificationLoad = fetch('/api/system/notifications', { headers: { Accept: 'application/json' } })
        .then(function (response) {
            if (!response.ok || response.redirected) throw new Error('HTTP ' + response.status);
            // Before setup is complete the server can return the setup page instead of a notice list.
            if (response.headers.get('content-type')?.includes('text/html')) throw new Error('Setup page');
            return response.json();
        })
        .then(function (data) {
            if (!Array.isArray(data?.notifications)) throw new Error('Invalid notification response');
            renderNotifications(data.notifications);
            failure.hidden = true;
            delete trigger.dataset.noticeState;
            return true;
        })
        .catch(function () {
            // Keep any last-known notices, but never claim the list is current or empty after a failure.
            document.getElementById('notificationEmpty').classList.add('d-none');
            failure.hidden = false;
            trigger.dataset.noticeState = 'error';
            trigger.setAttribute('aria-label', 'Recent activity, notices unavailable');
            trigger.title = 'Recent activity, notices unavailable';
            return false;
        })
        .finally(function () {
            loading.hidden = true;
            list.setAttribute('aria-busy', 'false');
            notificationLoad = null;
        });
    return notificationLoad;
}

function renderNotifications(notifications) {
    var list = document.getElementById('notificationList');
    var empty = document.getElementById('notificationEmpty');
    var badge = document.getElementById('notificationBellBadge');
    var trigger = document.getElementById('notificationBellBtn');
    var countPill = document.getElementById('notificationCount');
    if (!list) return;

    var bulk = document.getElementById('notificationDismissAllBtn');
    if (bulk) bulk.hidden = !notifications.some(function (notice) { return notice.dismissable !== false; });

    var count = notifications.length;
    if (empty) empty.classList.toggle('d-none', count !== 0);
    if (countPill) {
        countPill.textContent = String(count);
        countPill.classList.toggle('d-none', count === 0);
    }
    if (badge) {
        badge.textContent = count > 9 ? '9+' : String(count);
        badge.classList.toggle('d-none', count === 0);
    }
    if (trigger) {
        var label = count ? 'Recent activity, ' + count + (count === 1 ? ' notice' : ' notices') : 'Recent activity';
        trigger.setAttribute('aria-label', label);
        trigger.title = label;
    }
    var snapshot = JSON.stringify(notifications);
    if (snapshot === notificationSnapshot) return;
    notificationSnapshot = snapshot;
    if (list.contains(document.activeElement)) document.getElementById('rpActivityTitle')?.focus({ preventScroll: true });
    list.querySelectorAll('.notification-entry').forEach(function (el) { el.remove(); });

    notifications.forEach(function (notif) {
        var entry = document.createElement('div');
        var severity = notif.severity || 'info';
        entry.className = 'notification-entry ov-nt ov-nt-' + (['warning', 'error'].indexOf(severity) >= 0 ? severity : 'info');
        entry.dataset.notificationId = notif.id;

        var iconClass = severity === 'warning'
            ? 'bi-exclamation-triangle-fill text-warning'
            : severity === 'error'
                ? 'bi-x-octagon-fill text-danger'
                : 'bi-info-circle-fill text-info';

        var header = document.createElement('div');
        header.className = 'd-flex align-items-start';
        header.innerHTML =
            '<i class="bi ' + iconClass + ' me-2 mt-1 flex-shrink-0"></i>' +
            '<div class="flex-grow-1">' +
            '<strong class="d-block notification-title">' + escapeHtml(notif.title || 'Notification') + '</strong>' +
            '<button type="button" class="btn btn-link p-0 small notification-toggle-details" aria-expanded="false">Show details</button>' +
            '</div>';

        var details = document.createElement('div');
        details.className = 'notification-details small mt-2 d-none';
        details.innerHTML = sanitizeNotificationHtml(notif.body_html);

        var copyRow = document.createElement('div');
        copyRow.className = 'mt-2 d-none notification-copy-row';
        if (notif.source === 'vulkan_probe') {
            var copyBtn = document.createElement('button');
            copyBtn.type = 'button';
            copyBtn.className = 'btn btn-sm btn-outline-secondary';
            copyBtn.innerHTML = '<i class="bi bi-clipboard me-1"></i>Copy diagnostic bundle';
            copyBtn.onclick = function () { copyVulkanDiagnosticBundle(copyBtn); };
            copyRow.appendChild(copyBtn);
            copyRow.classList.remove('d-none');
        }

        var actions = document.createElement('div');
        actions.className = 'd-flex flex-wrap gap-2 mt-2';
        // An optional in-app link shown as a button, so the fix isn't hidden behind "Show details".
        var action = notif.action;
        if (action && typeof action.href === 'string' && action.href.charAt(0) === '/' && action.href.charAt(1) !== '/') {
            var go = document.createElement('a');
            go.className = 'btn btn-sm btn-outline-warning';
            go.href = action.href;
            go.textContent = action.label || 'Open';
            actions.appendChild(go);
        }
        if (notif.dismissable !== false) {
            var dismiss = document.createElement('button');
            dismiss.type = 'button';
            dismiss.className = 'btn btn-sm btn-outline-secondary ov-nt-dismiss';
            dismiss.textContent = 'Dismiss';
            dismiss.title = 'Hide until next restart';
            dismiss.onclick = function () { dismissNotification(notif.id, false); };
            actions.appendChild(dismiss);

            // A card for a problem that comes and goes (an unmounted media folder) is session-only.
            if (notif.permanent_dismissable !== false) {
                var dismissPerm = document.createElement('button');
                dismissPerm.type = 'button';
                dismissPerm.className = 'btn btn-sm btn-outline-danger ov-nt-perm';
                dismissPerm.textContent = 'Dismiss permanently';
                dismissPerm.title = 'Never show this notification again';
                dismissPerm.onclick = function () { dismissNotification(notif.id, true); };
                actions.appendChild(dismissPerm);
            }
        }

        entry.appendChild(header);
        entry.appendChild(details);
        entry.appendChild(copyRow);
        entry.appendChild(actions);
        list.appendChild(entry);

        // Wire "Show details" toggle.
        var toggle = entry.querySelector('.notification-toggle-details');
        if (toggle) {
            toggle.addEventListener('click', function () {
                var hidden = details.classList.toggle('d-none');
                toggle.textContent = hidden ? 'Show details' : 'Hide details';
                toggle.setAttribute('aria-expanded', hidden ? 'false' : 'true');
            });
        }
    });
}

async function updateNotification(path) {
    var response = await fetch('/api/system/notifications/' + path, { method: 'POST' });
    if (response.redirected || response.status === 401) throw new Error('Sign in again to update notices.');
    var data = response.headers.get('content-type')?.includes('application/json') ? await response.json() : null;
    if (!response.ok || data?.ok !== true) throw new Error(data?.error || 'The server did not confirm the update.');
}

async function performNotificationAction(action) {
    if (notificationMutation) return;
    notificationMutation = true;
    var panel = document.getElementById('rpActivity');
    var status = document.getElementById('notificationActionStatus');
    var controls = '#notificationDismissAllBtn, #notificationResetBtn, .ov-nt-dismiss, .ov-nt-perm';
    panel.querySelectorAll(controls).forEach(function (control) { control.setAttribute('aria-disabled', 'true'); });
    status.textContent = 'Updating notices…';
    status.hidden = false;
    try {
        var message = await action();
        // A poll that started before dismissal may have fetched the old list. Wait for it, then load the saved state.
        if (notificationLoad) await notificationLoad;
        var refreshed = await loadNotifications();
        status.textContent = message + (refreshed ? '' : ' The list could not refresh.');
    } catch (err) {
        status.textContent = 'Could not update notices: ' + err.message;
    } finally {
        notificationMutation = false;
        panel.querySelectorAll(controls).forEach(function (control) { control.removeAttribute('aria-disabled'); });
    }
}

function dismissNotification(id, permanent) {
    performNotificationAction(async function () {
        try {
            await updateNotification(encodeURIComponent(id) + (permanent ? '/dismiss-permanent' : '/dismiss'));
            return permanent ? 'Notice dismissed permanently.' : 'Notice dismissed until restart.';
        } catch (err) {
            return 'Could not dismiss the notice: ' + err.message;
        }
    });
}

document.addEventListener('DOMContentLoaded', function () {
    var panel = document.getElementById('rpActivity');
    if (!panel) return;
    panel.addEventListener('rp:popup-open', function () { if (!notificationMutation) loadNotifications(); });
    document.getElementById('notificationRetryBtn').addEventListener('click', loadNotifications);
    document.getElementById('notificationResetBtn').addEventListener('click', function () {
        performNotificationAction(async function () {
            try {
                await updateNotification('reset-dismissed');
                return 'Dismissed notices restored.';
            } catch (err) {
                return 'Could not restore dismissed notices: ' + err.message;
            }
        });
    });
    document.getElementById('notificationDismissAllBtn').addEventListener('click', function () {
        var entries = Array.from(panel.querySelectorAll('.notification-entry:has(.ov-nt-dismiss)'));
        if (!entries.length) return;
        performNotificationAction(async function () {
            var results = await Promise.allSettled(entries.map(function (entry) {
                return updateNotification(encodeURIComponent(entry.dataset.notificationId) + '/dismiss');
            }));
            var failed = results.filter(function (result) { return result.status === 'rejected'; }).length;
            var dismissed = results.length - failed;
            if (!failed) return 'Dismissed ' + dismissed + (dismissed === 1 ? ' notice.' : ' notices.');
            if (!dismissed) return 'Could not dismiss these notices. Try again.';
            return 'Dismissed ' + dismissed + '. Could not dismiss ' + failed + '.';
        });
    });
    loadNotifications();
    setInterval(function () {
        if (!document.hidden && !notificationMutation) loadNotifications();
    }, 60000);
});
