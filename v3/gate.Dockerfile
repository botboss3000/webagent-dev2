FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1100 v3 \
    && useradd -u 1102 -g 1100 -M -s /usr/sbin/nologin v3gate
COPY v3/gate_worker.py /opt/v3/gate_worker.py
COPY v3/plugins/gates/publication_size_guard_v1 /opt/v3/plugins/gates/publication_size_guard_v1
COPY v3/tests/test_gate_worker.py /opt/v3/tests/test_gate_worker.py
RUN python3 -B -m unittest discover -s /opt/v3/tests -p test_gate_worker.py
USER 1102:1100
ENV V3_GATE_SOCKET=/run/v3/gate/gate.sock
HEALTHCHECK --interval=10s --timeout=2s --start-period=5s --retries=3 \
    CMD python3 -c 'import os,socket; p=os.environ["V3_GATE_SOCKET"]; assert os.path.exists(p) and socket.socket(socket.AF_UNIX).connect_ex(p)==0'
CMD ["/usr/bin/python3","-I","/opt/v3/gate_worker.py"]
