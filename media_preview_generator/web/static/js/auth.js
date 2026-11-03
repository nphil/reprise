// Sign-in and first-run pages: the Show/Hide buttons on password fields, and the first-run form's own checks (length and
// "both passwords match") so the message names the problem instead of a browser tooltip. The server checks everything
// again; this only saves a round trip.
(function () {
    'use strict';

    var MIN_LENGTH = 8;

    document.querySelectorAll('[data-reveal]').forEach(function (button) {
        var fields = button.getAttribute('data-reveal').split(' ').map(function (id) { return document.getElementById(id); }).filter(Boolean);
        button.setAttribute('aria-label', 'Show password');
        button.addEventListener('click', function () {
            var show = button.getAttribute('aria-pressed') !== 'true';
            fields.forEach(function (field) { field.type = show ? 'text' : 'password'; });
            button.setAttribute('aria-pressed', show ? 'true' : 'false');
            button.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
            button.textContent = show ? 'Hide' : 'Show';
            if (fields[0]) fields[0].focus();
        });
    });

    var form = document.querySelector('[data-auth-form="create"]');
    if (!form) return;
    var serverError = document.getElementById('authError');
    var clientError = document.getElementById('authClientError');

    function problem() {
        var username = form.elements.username.value.trim();
        var password = form.elements.password.value;
        if (!username) return ['username', 'Choose a username.'];
        if (password.length < MIN_LENGTH) return ['password', 'Use at least ' + MIN_LENGTH + ' characters for the password.'];
        if (password !== form.elements.confirm_password.value) return ['confirm_password', "The two passwords don't match."];
        return null;
    }

    form.addEventListener('submit', function (event) {
        var found = problem();
        Array.prototype.forEach.call(form.elements, function (field) { field.removeAttribute('aria-invalid'); });
        if (!found) return;
        event.preventDefault();
        if (serverError) serverError.hidden = true;
        clientError.textContent = found[1];
        clientError.hidden = false;
        form.elements[found[0]].setAttribute('aria-invalid', 'true');
        form.elements[found[0]].focus();
    });
})();
