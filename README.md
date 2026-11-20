# Gazi

B2B cold outreach for exporters: import buyers (Apollo CSV or Apollo search), let the AI write each buyer their own
sequence, send it through Instantly (email) or HeyReach (LinkedIn), and answer replies from one desk.

## Run it on your PC (Windows)

You need Docker Desktop (running), Node.js 20+ and Git. Then, in PowerShell, one command at a time:

```
git clone https://github.com/mehedi2283/AG-SaaS.git
```
```
cd AG-SaaS
```
```
powershell -ExecutionPolicy Bypass -File infra/dev-setup.ps1
```

Add your own API keys to `infra\.env` (OpenAI, Stripe sandbox; see the table in the guide), then in a second window:

```
cd AG-SaaS/frontend
```
```
npm install
```
```
npm run dev
```

Open http://localhost:3000. The full guide, including making yourself the superuser and Stripe testing, is
[docs/runbooks/local-dev.md](docs/runbooks/local-dev.md).

## More

- [docs/status.md](docs/status.md): what's built, what's verified, what to do before going live
- [docs/runbooks/production.md](docs/runbooks/production.md): running it on a server
- [docs/architecture.md](docs/architecture.md): how the parts fit together
- [CLAUDE.md](CLAUDE.md): the rules every change follows
