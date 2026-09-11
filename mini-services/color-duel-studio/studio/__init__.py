"""Local-first Color Duel artwork authoring studio."""

# Single authoritative studio version (P0 hygiene: one constant, everything
# else derives from it - FastAPI app version, /api/config, the release
# archive name and the package.json files are kept in sync with it).
STUDIO_VERSION = '0.3.1'
