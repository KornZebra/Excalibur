# CTF-Agent 练习靶场镜像（仅用于本地演练，勿用于真实系统）
FROM python:3.12-slim

LABEL org.opencontainers.image.title="ctf-agent-practice-range" \
      org.opencontainers.image.description="故意存在漏洞的本地 CTF 练习靶机（SQL 注入 + 会话越权）"

WORKDIR /app
COPY app.py /app/app.py

ENV PORT=8080 \
    FLAG="flag{sql1_1nj3ct10n_b4s1cs}" \
    PYTHONUNBUFFERED=1

EXPOSE 8080
# 以非 root 运行（靶场本身有漏洞，但容器权限保持最小）
RUN useradd -m -u 10001 range && chown -R range:range /app
USER range

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/robots.txt',timeout=2)"

CMD ["python", "app.py"]