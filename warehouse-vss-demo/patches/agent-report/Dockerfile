# Build context: an isolated copy of the pinned NVIDIA source file with
# patches 0001 and 0002 applied in order.
FROM nvcr.io/nvidia/vss-core/vss-agent:3.2.1@sha256:b7f3246aaf355ebf96e91a40b2f0abc5dea7e330e7bf9a7cf726107b560c3ac1

USER 0
COPY --chown=0:0 --chmod=0444 services/agent/src/vss_agents/agents/report_agent.py /vss-agent/.venv/lib/python3.13/site-packages/vss_agents/agents/report_agent.py
USER 1000:1000
