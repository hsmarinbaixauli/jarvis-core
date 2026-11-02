# Jarvis en Docker (aislado en tu PC)

Jarvis corre dentro de Docker, con **sus propias cuentas** (Gmail, GitHub y Chrome solo de Jarvis) y sin
acceso a tu disco salvo dos carpetas: la de trabajo y la de memoria. Tus cuentas y tokens personales no
están a su alcance.

| Contenedor | Qué tiene | Qué NO tiene |
|---|---|---|
| `jarvis` | El núcleo, su base de datos y sus claves | Navegador, acceso a tu disco (solo `JarvisTrabajo` y `JarvisMemoria`) |
| `browser` | Chromium con el perfil de Jarvis | Claves, base de datos, puertos abiertos en tu PC |
| `login` (a demanda) | El mismo Chromium en una ventana noVNC, para iniciar sesión a mano | Nada más: se apaga al cerrar la ventana |

Los tres corren sin privilegios (usuario sin root, sistema de archivos de solo lectura y sin capacidades
del kernel). El chat web solo se publica en `127.0.0.1` de tu PC. Para verlo desde el móvil se usa
Tailscale, nunca un puerto abierto a Internet.

## 1. Preparar (una vez)

1. Instala **Docker Desktop** y, en *Settings → General*, marca **Start Docker Desktop when you sign in**.
   Así Jarvis arranca solo al encender el PC.
2. Si tenías la instalación antigua (sin Docker), quita el arranque automático:
   `.\scripts\windows\autostart.ps1 uninstall`
3. En la carpeta de Jarvis:
   ```powershell
   copy .env.example .env                                   # y edita las dos rutas
   copy docker\config\jarvis.yaml.example docker\config\jarvis.yaml
   copy tasks.example.yaml docker\config\tasks.yaml         # opcional
   docker compose --profile login build                     # tarda unos minutos la primera vez
   ```

## 2. Claves (dentro del contenedor, nunca en archivos tuyos)

```powershell
docker compose run --rm jarvis secrets set ANTHROPIC_API_KEY
docker compose run --rm jarvis secrets set TELEGRAM_BOT_TOKEN
docker compose run --rm jarvis secrets list
```

Se guardan en un volumen de Docker solo para el contenedor `jarvis`, con permisos únicamente para su
usuario. El navegador no las ve. Si cambias una clave con Jarvis en marcha: `docker compose restart jarvis`.

## 3. Cuentas de Jarvis

**Google (Gmail y Calendar de la cuenta de Jarvis).** Descarga el JSON del cliente OAuth de escritorio
como `docker\config\client_secret.json` y ejecuta:

```powershell
docker compose run --rm -p 127.0.0.1:8090:8090 jarvis google login /config/client_secret.json --port 8090 --no-browser
```

Abre el enlace que aparece **en una ventana de incógnito con la cuenta de Google de Jarvis** (no la tuya)
y autoriza. Después borra el JSON.

Para que Jarvis vea tu agenda, comparte tu calendario con su cuenta. Para que lea ciertos correos, crea
en tu Gmail un filtro que se los reenvíe. Así nunca tiene tu contraseña ni tu token.

**Navegador.** Para iniciar sesión a mano en las webs que Jarvis debe usar, con Jarvis parado:

```powershell
docker compose stop browser
docker compose --profile login run --rm --service-ports login
```

Abre `http://127.0.0.1:6080/vnc.html` y usa la contraseña que sale en la consola (es distinta cada vez).
Inicia sesión y cierra Chromium al terminar. Para abrir otra web distinta de Google:
`$env:LOGIN_URL="https://github.com/login"` antes del comando.

**GitHub.** Crea una cuenta de GitHub para Jarvis, invítala solo a los repos que elijas y guarda su
token: `docker compose run --rm jarvis secrets set GITHUB_TOKEN`.

## 4. Vault de Obsidian y voz (opcional)

**Vault.** Pon en `.env` `JARVIS_VAULT_DIR` con una carpeta **nueva** (no tu vault personal: Jarvis vería
todo lo que hay dentro). Puedes abrirla con Obsidian como un vault más. Jarvis busca y lee cualquier nota,
escribe con aviso solo dentro de `Jarvis/` y, para cambiar una nota tuya, te pide ✅.

**Voz.** Voicebox va **fuera** de Docker, en tu PC, porque usa tu tarjeta gráfica:

1. Instala Voicebox desde [voicebox.sh](https://voicebox.sh) y ábrelo. En *Settings* descarga el backend de
   CUDA (unos 3 GB) y reinícialo: `/health` debe decir `CUDA (NVIDIA …)`.
2. Crea un perfil de voz llamado **Jarvis**: una voz de serie (Kokoro → «Alex» o «Dora», en español) o tu voz
   clonada. Clona solo tu voz o una con permiso.
3. En `docker\config\jarvis.yaml`, sección `voice`: `enabled: true`. Después `docker compose restart jarvis`.
4. Mándale una nota de voz por Telegram: te escribe lo que ha entendido («🎙️ «…»»), lo hace y te contesta
   por texto y con una nota de voz. La primera vez descarga los modelos (Whisper y la voz): tarda unos minutos.

El audio va de Telegram al contenedor y de ahí a Voicebox en tu PC; nunca a otro servidor (la configuración
solo admite direcciones de este PC). Para que la voz vuelva tras reiniciar, abre Voicebox al iniciar sesión.

## 5. Arrancar y usar

```powershell
docker compose up -d
docker compose logs -f jarvis     # para ver qué hace (Ctrl+C sale del log, Jarvis sigue)
docker compose exec jarvis python -m jarvis web-url   # enlace del chat web
```

`restart: unless-stopped` hace que Jarvis vuelva solo tras un reinicio del PC (con Docker Desktop
arrancando al iniciar sesión). Al volver, te pregunta por Telegram si ejecuta las tareas programadas que
se perdieron.

| Quiero… | Comando |
|---|---|
| Pararlo todo | botón «🛑 Parar todo» en Telegram (Jarvis sigue encendido pero quieto) o `docker compose stop` |
| Actualizar | `git pull` y `docker compose up -d --build` |
| Ver las herramientas y su efecto | `docker compose run --rm jarvis tools` |
| Copia de seguridad de la base de datos | `docker compose cp jarvis:/data/jarvis.db .\copia-jarvis.db` |

## Comprobar que funciona (hazlo una vez)

- [ ] `docker compose ps`: `jarvis` y `browser` en estado *running*.
- [ ] Telegram: escribe «hola» y responde; aparece el botón «🛑 Parar todo». Púlsalo: «PARADA TOTAL activada».
      `/reanudar` lo quita.
- [ ] Chat web: el enlace de `docker compose exec jarvis python -m jarvis web-url` abre el chat;
      `http://127.0.0.1:8765/` en una ventana de incógnito **no** entra.
- [ ] Pídele «crea un archivo prueba.txt con un saludo»: aparece en tu carpeta `JarvisTrabajo`.
- [ ] Pídele «lee /etc/passwd»: responde que no tiene permiso.
- [ ] Pídele «abre github.com y dime qué pone»: usa el navegador del contenedor (con la sesión que iniciaste
      por noVNC, si la iniciaste).
- [ ] `docker compose run --rm jarvis tools`: lista todas las herramientas con su efecto.
- [ ] Reinicia el PC: Jarvis vuelve solo y te pregunta por las tareas programadas que se perdieran.
- [ ] Vault: «apunta en el vault una ficha de prueba»: aparece en `JarvisVault\Jarvis\` y recibes un aviso ℹ️.
      «Cambia mi nota X del vault»: te pide ✅.
- [ ] Voz: manda una nota de voz; ves «🎙️ «lo que dijiste»», la respuesta escrita y una nota de voz.
      Con Voicebox cerrado, te avisa de que no puede procesarla.

## Qué no cubre (todavía)

- `browser.credentials` (teclear contraseñas guardadas) no funciona con el navegador en contenedor:
  inicia sesión una vez por noVNC y el perfil recuerda la sesión.
- El sandbox del modo autónomo necesitaría acceso al Docker del PC, y eso equivale a darle el control de
  la máquina, así que en este modo queda desactivado. El modo autónomo tampoco usa el navegador.
- Desde los contenedores se puede llegar a servicios que tengas abiertos en tu PC
  (`host.docker.internal`): la voz lo usa a propósito para hablar con Voicebox (comprobado). Si tienes algo sensible escuchando en tu PC, avísame y lo cerramos con una
  red sin salida a la máquina.
- Las claves del volumen no van cifradas aparte: si alguien roba el disco, las protege el cifrado del
  disco. Activa BitLocker en Windows.
