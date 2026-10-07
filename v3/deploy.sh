#!/bin/sh
# Run only on the development server; never operates on v2 or production.
set -eu
cd /home/ar/webagent-dev2
case "${1:-build}" in
  build|build-loop)
    if [ "${1:-build}" = build-loop ]; then
      target=loop
      tag=webagent-dev-v3-loop:local
    else
      target=api
      tag=webagent-dev-v3:local
    fi
    # The complete allowlist enters both image targets from the same checkout.
    # Only the two named gate JSON files enter from runtime data.
    python3 -B -c 'import hashlib,json,pathlib; baseline=json.loads(pathlib.Path("v3/ui-baseline.json").read_text()); assert all(hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()==expected for path,expected in baseline["assets"].items()), "Shared UI baseline changed; review and explicitly update v3/ui-baseline.json"'
    # Gate config is protected state: tar may read only the two named JSON files.
    sudo -n tar --exclude=__pycache__ --exclude=*.pyc --exclude=*.pyo -cf - v3/Dockerfile v3/Cargo.toml v3/Cargo.lock v3/build.rs v3/.gitignore v3/src v3/ui v3/plugins/abilities v3/plugins/gates v3/worker.py v3/sandbox.py v3/launch.py v3/promote.py \
      v3/ui-baseline.json v3/tests v3/docs v3/README.md v3/deploy.sh v3/config \
      v3/data/config/gates/message_visibility_v1.json v3/data/config/gates/closing_status_v1.json \
      ui/shared/css/app1.css ui/shared/css/app2.css ui/shared/css/app3.css ui/shared/css/design-system.css ui/shared/css/index.css \
      ui/main-panel/agents/agents.html ui/main-panel/agents/agents.css ui/vendor/fonts ui/vendor/lucide ui/favicon.svg \
      | docker buildx build --builder default --load --progress=plain --target "$target" -f v3/Dockerfile -t "$tag" -
    ;;
  start)
    # COMPACTION CONFIG: this read-only directory supplies first-start migration.
    # After migration, both processes read /state/config/context-compaction.json.
    # The shared data-directory mount supports atomic runtime JSON replacement.
    if docker inspect webagent-dev-v3 >/dev/null 2>&1; then
      echo "webagent-dev-v3 already exists; inspect it before a deliberate lifecycle change" >&2
      exit 1
    fi
    mkdir -p v3/data v3/workspaces v3/runtime
    # Seed independent v3 chat configuration once; preserve subsequent v3 edits.
    mkdir -p v3/data/config
    if [ ! -f v3/data/config/chat.json ]; then
      cp data/config/chat_ui.json v3/data/config/chat.json
      python3 - <<'PYCONFIG'
import json
from pathlib import Path
path = Path('v3/data/config/chat.json')
config = json.loads(path.read_text())
config['chat_common']['execution_mode'] = {'default_mode': 'auto', 'confirm_auto_mode': False}
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PYCONFIG
    fi
    # Seed the documented admin storage policy once, never overwrite edits.
    if [ ! -f v3/data/config/app-settings.json ]; then
      cp v3/config/app-settings.json v3/data/config/app-settings.json
    fi
    docker run -d --name webagent-dev-v3 --restart unless-stopped --read-only \
      --cap-drop ALL --cap-add CHOWN --cap-add FOWNER --cap-add SETUID --cap-add SETGID \
      --security-opt no-new-privileges:true --pids-limit 256 --memory 2g --memory-swap 3g --cpus 2 \
      --publish 100.112.23.15:8081:8080 \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/data,dst=/state \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/config,dst=/opt/v3/config,readonly \
      --env V3_CONTEXT_COMPACTION_CONFIG_PATH=/opt/v3/config/context-compaction.json \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/workspaces,dst=/workspaces \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/runtime,dst=/run/v3 \
      --mount type=bind,src=/home/ar/webagent-dev2/v3,dst=/live-v3 \
      --mount type=bind,src=/home/ar/webagent-dev2,dst=/live-checkout \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/ui,dst=/opt/v3/ui,readonly \
      --env V3_CHAT_UI_CONFIG_PATH=/state/config/chat.json \
      --env V3_MESSAGE_API_TOKEN \
      --env V3_BROWSER_LOGIN_TOKEN \
      --env V3_BROWSER_LOGIN_CALLER_ID \
      --env V3_PROCESS_ROLE=api \
      --env V3_ABILITIES_ROOT=/live-v3/plugins/abilities \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/shared/css,dst=/opt/v3/shared-ui/shared/css,readonly \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/main-panel/agents,dst=/opt/v3/shared-ui/main-panel/agents,readonly \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/vendor/fonts,dst=/opt/v3/shared-ui/vendor/fonts,readonly \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/vendor/lucide,dst=/opt/v3/shared-ui/vendor/lucide,readonly \
      --mount type=bind,src=/home/ar/webagent-dev2/ui/favicon.svg,dst=/opt/v3/shared-ui/favicon.svg,readonly \
      --tmpfs /run:rw,nosuid,nodev,size=16m --tmpfs /tmp:rw,nosuid,nodev,size=32m \
      --stop-timeout 15 webagent-dev-v3:local
    ;;
  start-loop)
    # COMPACTION CONFIG: paired with start above; same legacy migration source
    # and /state authority. Existing data-root settings always win.
    if docker inspect webagent-dev-v3-loop >/dev/null 2>&1; then
      echo "webagent-dev-v3-loop already exists; inspect it before a deliberate lifecycle change" >&2
      exit 1
    fi
    docker exec --user 1100:1100 webagent-dev-v3 test -S /run/v3/worker/tools.sock && \
      docker exec --user 1100:1100 webagent-dev-v3 test -S /run/v3/promote/promote.sock || {
      echo "start the v3 API and worker container first" >&2
      exit 1
    }
    mkdir -p v3/gate-runtime
    docker run -d --name webagent-dev-v3-loop --restart unless-stopped --read-only \
      --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 --memory 768m --cpus 2 \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/data,dst=/state \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/config,dst=/opt/v3/config,readonly \
      --env V3_CONTEXT_COMPACTION_CONFIG_PATH=/opt/v3/config/context-compaction.json \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/workspaces,dst=/workspaces \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/runtime,dst=/run/v3 \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/gate-runtime,dst=/run/v3/gate \
      --mount type=bind,src=/home/ar/webagent-dev2/v3/plugins/abilities,dst=/opt/v3/plugins/abilities,readonly \
      --env V3_ABILITIES_ROOT=/opt/v3/plugins/abilities \
      --tmpfs /run:rw,nosuid,nodev,size=16m --tmpfs /tmp:rw,nosuid,nodev,size=32m \
      --stop-timeout 30 webagent-dev-v3-loop:local
    ;;
  *) echo "Usage: sh v3/deploy.sh build|start|build-loop|start-loop" >&2; exit 2 ;;
esac
