#!/bin/sh
# Run only on the development server; never operates on v2 or production.
set -eu
cd /home/ar/webagent-dev-v2
case "${1:-build}" in
  build)
    # Explicit allowlist works with both legacy Docker and BuildKit. No runtime data enters context.
    python3 -B -c 'import hashlib,json,pathlib; baseline=json.loads(pathlib.Path("v3/ui-baseline.json").read_text()); assert all(hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()==expected for path,expected in baseline["assets"].items()), "Shared UI baseline changed; review and explicitly update v3/ui-baseline.json"'
    tar -cf - v3/Dockerfile v3/Cargo.toml v3/Cargo.lock v3/src v3/ui v3/worker.py v3/launch.py \
      v3/ui-baseline.json \
      ui/shared/css/app1.css ui/shared/css/app2.css ui/shared/css/app3.css ui/shared/css/design-system.css ui/shared/css/index.css \
      ui/main-panel/agents/agents.html ui/main-panel/agents/agents.css ui/vendor/fonts ui/favicon.svg \
      | docker build -f v3/Dockerfile -t webagent-dev-v3:local -
    ;;
  start)
    if docker inspect webagent-dev-v3 >/dev/null 2>&1; then
      echo "webagent-dev-v3 already exists; inspect it before a deliberate lifecycle change" >&2
      exit 1
    fi
    mkdir -p v3/data v3/workspaces
    docker run -d --name webagent-dev-v3 --restart unless-stopped --read-only \
      --cap-drop ALL --cap-add CHOWN --cap-add FOWNER --cap-add SETUID --cap-add SETGID \
      --security-opt no-new-privileges:true --pids-limit 256 --memory 1g --cpus 2 \
      --publish 100.112.23.15:8081:8080 \
      --mount type=bind,src=/home/ar/webagent-dev-v2/v3/data,dst=/state \
      --mount type=bind,src=/home/ar/webagent-dev-v2/v3/workspaces,dst=/workspaces \
      --tmpfs /run:rw,nosuid,nodev,size=16m --tmpfs /tmp:rw,nosuid,nodev,size=32m \
      --stop-timeout 15 webagent-dev-v3:local
    ;;
  *) echo "Usage: sh v3/deploy.sh build|start" >&2; exit 2 ;;
esac
