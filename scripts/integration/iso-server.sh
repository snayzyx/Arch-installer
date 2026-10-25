#!/usr/bin/env bash
# Helpers for the tests of the "link" Windows source: an HTTPS server (nginx, so HTTP Range works)
# that serves one big file, with a throw-away certificate authority that the container trusts.
#
#   source iso-server.sh
#   start_iso_server /windows/Windows11.iso [rate-limit, e.g. 100m]
#   -> https://isoserver.test:8443/Win11.iso
#
# Needs a privileged Arch container with network access (pacman installs nginx and openssl).

ISO_SERVER_HOST=isoserver.test
ISO_SERVER_PORT=8443
ISO_SERVER_URL="https://${ISO_SERVER_HOST}:${ISO_SERVER_PORT}/Win11.iso"
ISO_SERVER_LOG_DIR="${ISO_SERVER_LOG_DIR:-/tmp/iso-server}"

start_iso_server() {
  local file="$1" rate="${2:-}"
  pacman -Sy --noconfirm --needed nginx openssl ca-certificates p11-kit >/dev/null 2>&1 \
    || { echo "cannot install nginx/openssl" >&2; return 1; }
  local dir=/etc/iso-server
  mkdir -p "$dir" /srv/iso "$ISO_SERVER_LOG_DIR"
  ln -sf "$file" /srv/iso/Win11.iso

  # A certificate authority of our own, and a server certificate for the test host name.
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=ArchInstall test CA" \
    -keyout "$dir/ca.key" -out "$dir/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=${ISO_SERVER_HOST}" \
    -keyout "$dir/server.key" -out "$dir/server.csr" 2>/dev/null
  printf 'subjectAltName=DNS:%s\nbasicConstraints=CA:FALSE\n' "$ISO_SERVER_HOST" >"$dir/server.ext"
  openssl x509 -req -in "$dir/server.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" -CAcreateserial -days 2 \
    -extfile "$dir/server.ext" -out "$dir/server.crt" 2>/dev/null

  # Trust the CA system wide, so that plain `curl` accepts the server (like a real certificate).
  install -m 644 "$dir/ca.crt" /etc/ca-certificates/trust-source/anchors/archinstall-test-ca.crt
  update-ca-trust
  grep -q "$ISO_SERVER_HOST" /etc/hosts || echo "127.0.0.1 $ISO_SERVER_HOST" >>/etc/hosts

  local limit=""
  [[ -n "$rate" ]] && limit="limit_rate $rate;"
  cat >"$dir/nginx.conf" <<EOF
worker_processes 1;
pid /run/iso-server-nginx.pid;
error_log $ISO_SERVER_LOG_DIR/error.log;
events { worker_connections 64; }
http {
  access_log $ISO_SERVER_LOG_DIR/access.log;
  sendfile on;
  server {
    listen ${ISO_SERVER_PORT} ssl;
    server_name ${ISO_SERVER_HOST};
    ssl_certificate $dir/server.crt;
    ssl_certificate_key $dir/server.key;
    location / { root /srv/iso; $limit }
  }
}
EOF
  nginx -c "$dir/nginx.conf" || { echo "nginx did not start" >&2; return 1; }
  local tries
  for tries in 1 2 3 4 5 6 7 8 9 10; do
    curl -sI --max-time 5 "$ISO_SERVER_URL" | grep -q '^HTTP/.* 200' && return 0
    sleep 1
  done
  echo "the HTTPS server does not answer" >&2
  return 1
}

stop_iso_server() {
  [[ -f /run/iso-server-nginx.pid ]] && kill "$(cat /run/iso-server-nginx.pid)" 2>/dev/null || true
}
