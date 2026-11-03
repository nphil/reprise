// Settings > Advanced > Account: the Change password form. The server decides what is acceptable and says why;
// the checks here only save a round trip for the obvious mistakes.
(function () {
    'use strict';

    var form = document.getElementById('changePasswordForm');
    if (!form) return;

    var MIN_LENGTH = 8;
    var current = document.getElementById('currentPassword');
    var fresh = document.getElementById('newPassword');
    var again = document.getElementById('newPasswordAgain');
    var button = document.getElementById('changePasswordBtn');
    var result = document.getElementById('passwordResult');

    function say(text, ok) {
        result.className = 'small ' + (ok ? 'text-success' : 'text-danger');
        result.textContent = text;
    }

    function fail(text, field) {
        say(text, false);
        field.focus();
    }

    form.addEventListener('submit', function (event) {
        event.preventDefault();
        result.textContent = '';
        if (!current.value) return fail('Enter your current password.', current);
        if (fresh.value.length < MIN_LENGTH) return fail('Use at least ' + MIN_LENGTH + ' characters for the new password.', fresh);
        if (fresh.value !== again.value) return fail("The two new passwords don't match.", again);
        button.disabled = true;
        fetch('/api/account/password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ current_password: current.value, new_password: fresh.value }),
            credentials: 'same-origin'
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (response.ok && data.success) {
                    form.reset();
                    say('Password changed. Every other browser was signed out; this one stays signed in.', true);
                } else if (response.status === 429) {
                    say('Too many attempts. Wait a minute, then try again.', false);
                } else {
                    say(data.error || 'Could not change the password.', false);
                }
            });
        }).catch(function () {
            say('Could not reach Reprise. Check the connection and try again.', false);
        }).then(function () {
            button.disabled = false;
        });
    });
})();
