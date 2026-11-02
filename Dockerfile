# syntax=docker/dockerfile:1
#
# eBiblio Catalunya MCP server (Odilo) image.
#
# Overlays this repository's source on the eBiblio runtime image, which already
# carries libgourou (Adobe ADEPT) and calibre for DRM export. No credentials are
# baked in: they arrive through environment variables at run time.

ARG EBIBLIO_BASE=mcp-ebiblio:local
FROM ${EBIBLIO_BASE}

LABEL org.opencontainers.image.title="mcp-ebiblio-catalunya" \
      org.opencontainers.image.description="MCP server for eBiblio Catalunya (Odilo): search, borrow, return and export loans" \
      org.opencontainers.image.source="https://github.com/legido-ai/mcp-ebiblio-catalunya"

COPY src/ /app/src/
ENV PYTHONPATH=/app/src

ENTRYPOINT ["python", "-m", "odilo_ebiblio"]
