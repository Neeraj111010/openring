# OpenRing

**Agentes de voz com IA, open source e no seu servidor, com API compatível com o Vapi. Sem taxa de plataforma.**

As plataformas de voz cobram uma taxa por minuto em cima do que você já paga de transcrição, modelo,
voz e telefonia. O OpenRing roda no seu servidor, com as suas chaves, e aceita o mesmo JSON dos
assistentes do Vapi: migrar é praticamente trocar a URL.

[English](README.md)

## Situação: v0.1 (início)

| Já funciona | Próximos passos |
|---|---|
| Ligações recebidas pelo Twilio | Ligações de saída (`POST /call`) |
| APIs `/assistant`, `/phone-number` e `/call` no formato do Vapi | Telnyx, Plivo, Vonage |
| Transcrição Deepgram; qualquer modelo compatível com OpenAI; voz ElevenLabs ou Kokoro (grátis, local) | Fallback e roteamento por custo entre provedores |
| `firstMessage`, `endCallPhrases` e webhook `end-of-call-report` | Ferramentas, painel, modo agência (multi-cliente, white-label) |

## Começo rápido

```bash
cp .env.example .env          # preencha suas chaves
docker compose up --build     # API em http://localhost:8000
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
