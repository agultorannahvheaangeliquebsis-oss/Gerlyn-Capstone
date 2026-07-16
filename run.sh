#!/usr/bin/env bash
#
# GerlynPLUS dev helper.
#
#   ./run.sh          start the server (kills any stale one first)
#   ./run.sh fresh    wipe + reload demo data, then start
#   ./run.sh stop     stop the server
#   ./run.sh test     run the acceptance tests
#
# Stop the server any time with Ctrl+C, or: ./run.sh stop

set -e
cd "$(dirname "$0")"

PORT=8000

# --- always use the project's virtualenv -----------------------------------
if [ ! -d ".venv" ]; then
  echo "No .venv found. Create one first:"
  echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi
source .venv/bin/activate

# --- free the port ---------------------------------------------------------
free_port() {
  local pids
  pids=$(lsof -ti:$PORT 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "Stopping the server already on port $PORT..."
    echo "$pids" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
}

# --- show the LAN address so you can test from a phone ---------------------
lan_ip() {
  ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo ""
}

case "${1:-start}" in
  stop)
    free_port
    echo "Server stopped."
    ;;

  test)
    echo "Running acceptance tests..."
    python manage.py test core
    ;;

  fresh|start)
    free_port

    if [ "${1:-start}" = "fresh" ]; then
      echo "Reloading demo data..."
      python manage.py migrate --noinput >/dev/null
      python manage.py seed_demo --reset
    fi

    IP=$(lan_ip)
    echo ""
    echo "  Storefront / login :  http://127.0.0.1:$PORT/"
    if [ -n "$IP" ]; then
      echo "  From your phone    :  http://$IP:$PORT/"
      echo "                        (if this fails, add $IP to DJANGO_ALLOWED_HOSTS in .env)"
    fi
    echo "  Accounts           :  gerlyn / carlo / ana / juan   — password: demo1234"
    echo ""
    echo "  Press Ctrl+C to stop."
    echo ""
    exec python manage.py runserver 0.0.0.0:$PORT
    ;;

  *)
    echo "Usage: ./run.sh [start|fresh|stop|test]"
    exit 1
    ;;
esac
