# Painel de Convênios e Parcerias do RS

Painel de busca dos convênios e parcerias celebrados pelo Estado do Rio Grande do Sul, a partir da consulta pública do
Portal de Convênios e Parcerias RS (sistema FPE, CAGE/Sefaz).

- `scripts/coletar.py` consulta o portal (sem login), divide por município quando a consulta bate no teto de 1.000 linhas
  e busca, aos poucos, data de assinatura, situação e objeto completo na tela de detalhe.
- O GitHub Actions roda todo dia, atualiza `docs/data.json` e publica o painel no GitHub Pages.
- `docs/novos.json` lista os instrumentos detectados nos últimos 30 dias (campo `dt` = data da detecção).
- A primeira carga forma a base e não gera aviso; só o que aparecer depois é tratado como novo.
- Áreas temáticas: palavras do objeto, em `scripts/areas.py`.

Coleta manual: aba **Actions**, fluxo **Atualizar painel**, **Run workflow**. Para a carga inicial, informe os anos
`2023 2024 2025 2026`.
