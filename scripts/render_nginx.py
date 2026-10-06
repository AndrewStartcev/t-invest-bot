"""Generate one isolated nginx vhost; HTTP never exposes tokens or a desktop."""
import argparse
import re
from pathlib import Path


def validate_domain(value):
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value):
        raise ValueError("Некорректный домен")
    return value


def render(domain, https=False, shared_source=False):
    domain = validate_domain(domain)
    http = f'''server {{
    listen 80;
    server_name {domain};
    location ^~ /.well-known/acme-challenge/ {{
        root /var/www/t-invest-bot-acme;
        auth_basic off;
    }}
    location / {{ {"return 301 https://" + domain + "$request_uri;" if https else "return 503;"} }}
}}
'''
    if not https:
        return http
    owner_auth = ('auth_basic "Pulse source owner";\n        auth_basic_user_file /etc/nginx/t-invest-source.htpasswd;' if shared_source else "")
    owner_location = '''
    location ^~ /source-admin {
        auth_basic "Pulse source owner";
        auth_basic_user_file /etc/nginx/t-invest-source.htpasswd;
        include /etc/t-invest-bot/source-proxy.conf;
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header Authorization "";
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
    }
''' if shared_source else ""
    return http + f'''
server {{
    listen 443 ssl;
    server_name {domain};
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    auth_basic "T-Invest Bot";
    auth_basic_user_file /etc/nginx/t-invest-bot.htpasswd;
    client_max_body_size 16k;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;

    {owner_location}
    location = /desktop/websockify {{
        {owner_auth}
        proxy_pass http://127.0.0.1:6080/websockify;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header Authorization "";
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }}
    location /desktop/ {{
        {owner_auth}
        alias /usr/share/novnc/;
        index vnc.html;
        include /etc/nginx/mime.types;
        autoindex off;
    }}
    location / {{
        proxy_set_header X-TInvest-Source-Key "";
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header Authorization "";
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
        proxy_buffering off;
    }}
}}
'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain")
    parser.add_argument("--https", action="store_true")
    parser.add_argument("--shared-source", action="store_true")
    args = parser.parse_args()
    shared = args.shared_source or Path("/etc/t-invest-bot/source-proxy.conf").exists()
    print(render(args.domain, args.https, shared), end="")
