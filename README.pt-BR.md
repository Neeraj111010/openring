# OpenRing

**Agentes de voz com IA, open source e no seu servidor, com API compatível com o Vapi. Sem taxa de plataforma.**

As plataformas de voz cobram uma taxa por minuto em cima do que você já paga de transcrição, modelo,
voz e telefonia. O OpenRing roda no seu servidor, com as suas chaves, e aceita o mesmo JSON dos
assistentes do Vapi: migrar é praticamente trocar a URL.

[English](README.md)

## Situação: v0.1 (início)

| Já funciona | Próximos passos |
|---|---|
| Ligações recebidas e feitas (`POST /call`) pelo Twilio | |
| Teste no navegador pelo painel (WebRTC, sem precisar de número) | |
| APIs `/assistant`, `/phone-number` e `/call` no formato do Vapi | Telnyx, Plivo, Vonage |
| Transcrição Deepgram; qualquer modelo compatível com OpenAI; voz ElevenLabs ou Kokoro (grátis, local) | Fallback e roteamento por custo entre provedores |
| `firstMessage`, `endCallPhrases` e webhook `end-of-call-report` | Modo agência (multi-cliente, white-label) |
| Ferramentas no formato do Vapi: `function` (seu servidor recebe `tool-calls`), `endCall`, `transferCall`, API `/tool` | Mais tipos de ferramenta (DTMF, base de conhecimento) |

## Começo rápido

Um comando só. Ele instala tudo o que precisa (inclusive o Python), sobe o servidor e abre o painel:

```bash
npx github:elifalanrocha/openring
```

Basta ter o [Node.js](https://nodejs.org) 18+. As configurações ficam em `~/.openring/.env`, criado na primeira execução com uma chave gerada.
No primeiro acesso, o painel pede para você criar a senha dele. A chave da API continua valendo para os apps
que chamam a API (ela aparece em **Settings**). Esqueceu a senha? `openring --reset-password`.

Converse com um assistente antes de comprar um número: clique em **Test** ao lado dele no painel e
fale no microfone. Antes, coloque no `~/.openring/.env` uma chave do Deepgram e uma de modelo (o plano
grátis do Groq serve, por exemplo com `openai/gpt-oss-20b`). A voz grátis Kokoro se baixa sozinha na
primeira execução (cerca de 120 MB).

### Com Docker

```bash
cp .env.example .env          # preencha suas chaves
docker compose up --build     # API em http://localhost:8000; defina OPENRING_PASSWORD no .env para o painel
ngrok http 8000               # coloque a URL https em PUBLIC_URL e reinicie
```

Crie um assistente em português:

```bash
curl -X POST http://localhost:8000/assistant \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "Recepção",
    "firstMessage": "Olá! Como posso ajudar?",
    "model": {"provider": "openai", "model": "gpt-4.1-mini",
              "messages": [{"role": "system", "content": "Você é uma recepcionista simpática. Responda em português."}]},
    "voice": {"provider": "11labs", "voiceId": "SEU_VOICE_ID"},
    "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "pt-BR"}
  }'
```

Cadastre o número do Twilio e aponte o **Voice webhook** (HTTP POST) dele para
`$PUBLIC_URL/twilio/voice`:

```bash
curl -X POST http://localhost:8000/phone-number \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{"number": "+15551234567", "assistantId": "ID_DO_ASSISTENTE"}'
```

Ligue para o número. As transcrições ficam em `GET /call`.

### Ferramentas

As ferramentas usam o formato do Vapi: um servidor feito para o Vapi funciona sem mudança. Coloque-as em
`model.tools` (ou salve com `POST /tool` e liste os ids em `model.toolIds`). Quando o modelo chama uma
ferramenta `function`, o OpenRing envia `{"message": {"type": "tool-calls", ...}}` para o `server.url` dela
(ou do assistente) e lê de volta `{"results": [{"toolCallId": "...", "result": "..."}]}`. `endCall` desliga
depois da despedida; `transferCall` transfere a ligação (Twilio) para o número escolhido quando o assistente
termina de falar. Exemplo completo no [README em inglês](README.md#tools).

## Stack grátis

| Camada | Opção grátis |
|---|---|
| Transcrição | Deepgram: US$ 200 de crédito para contas novas |
| Modelo | `"provider": "omniroute"` (gateway local grátis) ou `"provider": "groq"` |
| Voz | `"provider": "kokoro"`, roda local na CPU, sem chave |
| Telefonia | Crédito de teste do Twilio |

## Desenvolvimento

```bash
pip install -e ".[dev,kokoro]"
pytest
python -m openring
```

## Licença

MIT. Sem vínculo com Vapi, Retell ou Twilio.
