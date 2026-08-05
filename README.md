# tubedata-mcp

Zdalny serwer MCP bazy wiedzy tubedata (Qdrant + fastembed) za Bearer tokenem.
Deploy: Coolify (docker-compose). Sekrety wyłącznie przez env (MCP_TOKEN).

Klient (Claude Code):
    claude mcp add tubedata-kb --transport http https://kbdata.devince.dev/mcp \
      --header "Authorization: Bearer <MCP_TOKEN>"
