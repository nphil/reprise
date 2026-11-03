"""Signing a test client in as the owner, the way the app itself counts a browser as signed in.

The app only trusts a session whose ``epoch`` matches the owner account's, so a test that wants a signed-in browser
makes the account (if the test's config folder has none) and puts that epoch in the client's session. Nothing here
goes through the sign-in form, so it works with CSRF on or off and never touches the sign-in rate limit.
"""

OWNER_USERNAME = "owner"
OWNER_PASSWORD = "owner-password-1"


def ensure_owner_account(username: str = OWNER_USERNAME, password: str = OWNER_PASSWORD) -> None:
    """Make the owner account in the current ``auth.AUTH_FILE`` unless it already exists."""
    from media_preview_generator.web import auth

    if not auth.account_exists():
        auth.create_account(username, password)


def sign_in(client, username: str = OWNER_USERNAME, password: str = OWNER_PASSWORD):
    """Make ``client`` a signed-in browser (creating the owner account first when the test has none yet).

    The first-run tour counts as skipped: it opens by itself over a new account's pages, and a browser test that shares
    this session (the ``authed_client`` cookie handed to Playwright) would find every click blocked by it. Tests of the
    tour itself make their account through the Welcome page instead.
    """
    from media_preview_generator.web import auth

    ensure_owner_account(username, password)
    auth.set_onboarding_state(auth.ONBOARDING_DISMISSED)
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["epoch"] = auth.session_epoch()
    return client
