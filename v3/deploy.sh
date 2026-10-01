#!/bin/sh
# Run only on the development server; never operates on v2 or production.
set -eu
cd /home/ar/webagent-dev2
case "${1:-build}" in
  build)
    # Explicit allowlist works with both legacy Docker and BuildKit. No runtime data enters context.
    python3 -B -c 'import hashlib,json,pathlib; baseline=json.loads(pathlib.Path("v3/ui-baseline.json").read_text()); assert all(hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()==expected for path,expected in baseline["assets"].items()), "Shared UI baseline changed; review and explicitly update v3/ui-baseline.json"'
    tar -cf - v3/Dockerfile v3/Cargo.toml v3/Cargo.lock v3/.gitignore v3/src v3/ui v3/plugins/abilities v3/worker.py v3/launch.py v3/promote.py \
      v3/ui-baseline.json v3/tests v3/README.md v3/deploy.sh \
      ui/shared/css/app1.css ui/shared/css/app2.css ui/shared/css/app3.css ui/shared/css/design-system.css ui/shared/css/index.css \
      ui/main-panel/agents/agents.html ui/main-panel/agents/agents.css ui/vendor/fonts ui/vendor/lucide ui/favicon.svg \
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
      --mount type=bind,src=/home/ar/webagent-dev2/v3/data,dst=/state \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/workspaces,dst=/workspaces \
      --mount type=bind,src=/home/ar/webagent-dev2/v3,dst=/live-v3 \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/ui,dst=/opt/v3/ui,readonly \
      --mount type=bind,src=/home/ar/webagent-dev2/data/config/chat_ui.json,dst=/opt/v3/chat_ui.json,readonly \
      --env V3_CHAT_UI_CONFIG_PATH=/opt/v3/chat_ui.json \
      --env V3_MESSAGE_API_TOKEN \
      --env V3_BROWSER_LOGIN_TOKEN \
      --env V3_BROWSER_LOGIN_CALLER_ID \
      --env V3_ABILITIES_ROOT=/live-v3/plugins/abilities \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/shared/css,dst=/opt/v3/shared-ui/shared/css,readonly \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/main-panel/agents,dst=/opt/v3/shared-ui/main-panel/agents,readonly \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/vendor/fonts,dst=/opt/v3/shared-ui/vendor/fonts,readonly \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/vendor/lucide,dst=/opt/v3/shared-ui/vendor/lucide,readonly \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/favicon.svg,dst=/opt/v3/shared-ui/favicon.svg,readonly \
      --tmpfs /run:rw,nosuid,nodev,size=16m --tmpfs /tmp:rw,nosuid,nodev,size=32m \
      --stop-timeout 15 webagent-dev-v3:local
    ;;
  *) echo "Usage: sh v3/deploy.sh build|start" >&2; exit 2 ;;
esac
