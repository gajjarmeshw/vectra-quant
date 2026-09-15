# Multi-stage Dockerfile for VECTRA_QUANT (Vite PWA + FastAPI Python Backend)

# Stage 1: Build PWA Frontend
FROM node:20-alpine AS pwa-builder
WORKDIR /app/pwa
COPY pwa/package*.json ./
RUN npm ci
COPY pwa/ ./
RUN npm run build

# Stage 2: Production Python Backend
FROM python:3.11-slim
WORKDIR /app

# Install uv for ultra-fast dependency installation
RUN pip install --no-cache-dir uv

# Copy backend dependencies and source
COPY pyproject.toml ./
COPY backend/ ./backend/
COPY config/ ./config/

# Install python package and dependencies
RUN uv pip install --system -e .

# Copy built PWA static assets from Stage 1 into backend static folder
COPY --from=pwa-builder /app/pwa/dist ./pwa/dist

# Expose FastAPI port
EXPOSE 8000

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    PORT=8000

# Run Uvicorn production server
CMD ["uvicorn", "vectra_quant.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
