#!/usr/bin/env python3
"""Query a saved Reprise result without transmitting credentials in the URL."""
import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

FIELDS = ('plex_machine_identifier', 'rating_key', 'media_id', 'part_id', 'part_key', 'part_size_bytes', 'duration_ms', 'audio_stream_id')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base_url', help='Reprise server base URL')
    for field in FIELDS:
        parser.add_argument('--' + field.replace('_', '-'), required=True, type=int if field in ('part_size_bytes', 'duration_ms') else str)
    args = parser.parse_args()
    token = os.environ.get('REPRISE_TOKEN')
    url = urlsplit(args.base_url)
    if not token or url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password or url.query or url.fragment:
        parser.error('Set REPRISE_TOKEN and provide an HTTP(S) base URL without credentials, query or fragment')
    identity = {field: getattr(args, field) for field in FIELDS}
    if '?' in args.part_key or '#' in args.part_key:
        parser.error('part-key must not contain a query or fragment')
    req = Request(args.base_url.rstrip('/') + '/api/v1/results?' + urlencode(identity), headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    try:
        with build_opener(NoRedirect).open(req, timeout=15) as response:
            value = json.load(response)
            print(json.dumps(value, indent=2))
    except HTTPError as exc:
        print(f'Request failed (HTTP {exc.code})', file=sys.stderr)
        return 1
    except (URLError, ValueError, OSError):
        print('Request failed or returned invalid JSON', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
