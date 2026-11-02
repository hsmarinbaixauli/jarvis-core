# Instalar Jarvis en tu PC (Windows), paso a paso

Tiempo: unos 45 minutos la primera vez. No hace falta saber programar: solo copiar y pegar comandos.

> **Antes de nada:** nunca pegues una clave o contraseña en un chat, en un correo ni en un archivo.
> Las claves se guardan con un comando (`python -m jarvis secrets set NOMBRE`) que las esconde en el
> Administrador de credenciales de Windows. Si alguien (o algo) te pide una clave por chat, no la des.

## 0. Qué vas a necesitar

| Qué | Para qué | Coste |
|---|---|---|
| Cuenta en [console.anthropic.com](https://console.anthropic.com) con saldo | El cerebro de Jarvis | Pago por uso (pon un límite en la consola) |
| Telegram en el móvil | Hablar con Jarvis y aprobar acciones | Gratis |
| Cuenta de Google | Gmail y Calendar | Gratis |
| (Opcional) Cuenta de GitHub | Que trabaje en tus repos | Gratis |
| (Opcional) Google Places API | Playbooks que buscan negocios | Tiene cuota gratuita mensual |

## 1. Descargar Jarvis

1. Instala **Git**: abre PowerShell (tecla Windows → escribe `PowerShell` → Enter) y ejecuta
   `winget install --id Git.Git -e`. Cierra y vuelve a abrir PowerShell.
2. Elige una carpeta (por ejemplo tus Documentos) y descarga el proyecto:

   ```powershell
   cd $HOME\Documents
   git clone https://github.com/hsmarinbaixauli/jarvis-core.git
   cd jarvis-core
   ```

## 2. Instalador automático

En esa misma ventana:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
```

El instalador comprueba Python 3.12, Node.js y Google Chrome (te ofrece instalarlos), prepara el entorno, crea
`jarvis.yaml` y `tasks.yaml` y te pide las dos claves básicas. Si te dice que cierres la ventana y lo repitas
(pasa tras instalar Python), hazlo: es normal. Puedes ejecutarlo tantas veces como quieras.

Las claves que te pide:

- **ANTHROPIC_API_KEY** → en console.anthropic.com: *API Keys → Create Key*. Copia la clave, vuelve a
  PowerShell, pégala con clic derecho y pulsa Enter (no verás nada al pegar: es normal).
  Recomendado: en *Settings → Limits* pon un límite mensual de gasto.
- **TELEGRAM_BOT_TOKEN** → en Telegram abre [@BotFather](https://t.me/BotFather), escribe `/newbot`,
  ponle nombre y te dará un token. Pégalo igual.

## 3. Tu chat de Telegram

1. Arranca Jarvis a mano: `.\.venv\Scripts\python.exe -m jarvis run`
2. En Telegram, abre tu bot y escríbele «hola». Te contestará con tu **chat id** (un número).
3. Pulsa `Ctrl+C` en PowerShell para pararlo. Abre `jarvis.yaml` con el Bloc de notas
   (`notepad jarvis.yaml`) y pon el número en:

   ```yaml
   telegram:
     allowed_chat_id: 123456789
   ```

4. Vuelve a arrancar. Ahora Jarvis solo hará caso a tu chat: cualquier otra persona que encuentre tu bot
   será ignorada.

## 4. Elegir qué carpetas puede tocar

En `jarvis.yaml`, `allowed_dirs` son las únicas carpetas donde Jarvis puede leer y escribir. Por defecto
es `~/JarvisTrabajo` (se crea sola). Añade otras solo si lo necesitas; **no pongas** tu carpeta de usuario
entera ni la de Jarvis.

## 5. Conectar servicios (opcional, en este orden)

Sigue las secciones del [README](../README.md):

1. **Navegador** — *Conectar el navegador ("ojos")*: `python -m jarvis browser login` e inicia sesión a
   mano en tus webs (recomendado: una cuenta de Google solo para Jarvis). Jarvis nunca ve contraseñas;
   para webs con contraseña propia, registra credenciales limitadas a su dominio.
2. **Gmail y Calendar** — *Conectar Gmail y Calendar* (crear un cliente OAuth de escritorio).
3. **GitHub** — *Conectar GitHub* (token fine-grained solo para los repos que elijas).
4. **Playbooks y extensiones** (opcional) — los tuyos viven en otra carpeta; ver `playbooks/README.md`.

Después de cada uno ejecuta `.\.venv\Scripts\python.exe -m jarvis doctor`: todo lo que uses debe salir con ✓.

## 6. Que arranque solo

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\autostart.ps1 install
```

Crea una tarea programada llamada **Jarvis** que lo arranca, sin ventanas, cada vez que inicias sesión, y lo
reinicia si se cae (si se cae más de 5 veces en una hora, para y te avisa por Telegram).

| Quiero… | Comando (desde la carpeta de Jarvis) |
|---|---|
| Ver si está activo | `powershell -ExecutionPolicy Bypass -File scripts\windows\autostart.ps1 status` |
| Pararlo | `… autostart.ps1 stop` |
| Arrancarlo | `… autostart.ps1 start` |
| Quitar el arranque automático | `… autostart.ps1 uninstall` |
| Ver el enlace del chat web | `.\.venv\Scripts\python.exe -m jarvis web-url` |

Jarvis corre con tu usuario (no como servicio de Windows) porque necesita tus credenciales guardadas y tu
perfil del navegador. **Solo trabaja mientras el PC está encendido y con tu sesión iniciada**: en
*Configuración → Sistema → Inicio/apagado y suspensión* pon «Suspender: nunca» si quieres que trabaje 24/7.

## 7. Actualizar

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\autostart.ps1 stop
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m jarvis doctor
powershell -ExecutionPolicy Bypass -File scripts\windows\autostart.ps1 start
```

## 8. Si algo falla

| Síntoma | Qué hacer |
|---|---|
| «Falta la clave de Anthropic» | `python -m jarvis secrets set ANTHROPIC_API_KEY` |
| El bot no contesta | `autostart.ps1 status`; mira `data\logs\jarvis.log` y `data\logs\supervisor.log` |
| «Tu chat id es…» en cada mensaje | Falta `telegram.allowed_chat_id` en `jarvis.yaml` (paso 3) |
| El chat web dice que abras el enlace | `python -m jarvis web-url` y abre ese enlace (guárdalo en favoritos) |
| Gmail: «Google no está conectado» | Repite `python -m jarvis google login client_secret.json` |
| El navegador no inicia sesión | Para Jarvis y repite `python -m jarvis browser login` |
| «La credencial X solo se puede usar en…» | Es el bloqueo por dominio; añade el dominio en `browser.credentials` si es legítimo |
| `doctor` marca ✗ en Chrome | Instala Google Chrome desde google.com/chrome |
| «He llegado al límite de gasto de hoy» | Es el freno de seguridad; súbelo en `jarvis.yaml` → `budget.daily_usd` |
| `doctor` marca ✗ en Node.js | Instala Node LTS desde nodejs.org y abre una PowerShell nueva |

Los logs nunca contienen tus claves (se enmascaran), así que puedes compartirlos para pedir ayuda.

## 9. Desinstalar

1. `autostart.ps1 uninstall`
2. Borra las claves: `python -m jarvis secrets delete NOMBRE` para cada una de `python -m jarvis secrets list`
   (o en *Administrador de credenciales → Credenciales de Windows*, las que empiezan por `jarvis`).
3. Borra la carpeta de Jarvis. Revoca el acceso en tu cuenta de Google (*Seguridad → Acceso de terceros*) y
   el token de GitHub si lo creaste.
