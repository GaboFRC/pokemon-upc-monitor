# Monitor UPC Pokémon 30th Celebration — weplay.cl

Avisa cuando weplay.cl publique el **Pokémon TCG 30th Celebration Ultra-Premium Collection**
(Day / Espeon y Night / Umbreon). Lanzamiento oficial: **6 de noviembre de 2026**, puede
aparecer antes como preventa.

## Estado al 16 de septiembre de 2026

El UPC **todavía no está publicado**. Pero weplay ya tiene cargada la línea 30th Celebration
(ex Box, Poster Collection y Elite Trainer Box, en español e inglés, IDs 100061–100066), así
que el producto va a entrar por el mismo canal.

## Cómo funciona

Cada ciclo hace **4 peticiones** (una pausa aleatoria de 3–7 s entre cada una):

| # | Fuente | Para qué |
|---|--------|----------|
| 1 | `catalogsearch/result/?q=30th` | Detección principal. Trae nombre, URL, precio, stock y botón carrito en un solo HTML. |
| 2 | `catalogsearch/result/?q=ultra premium` | Red de seguridad por si cambian el nombre. |
| 3 | `cartas-y-mazos.html?brand_id=2049` | Listado de la marca Pokémon, por si el producto no calza con la búsqueda de texto. |
| 4 | `preventas.html` | Captura la preventa. |

**Coincidencia**: normaliza el texto (minúsculas, sin tildes) y exige `30th` **o** `celebration`,
junto con `ultra` **y** `premium`. Eso descarta el Elite Trainer Box 30th y también las pilas
Kodak "Ultra Premium" que hay en el catálogo. La variante Day/Night se deduce de
`day`/`espeon` y `night`/`umbreon`.

**Estados**: `en_stock`, `preventa`, `solo_en_tienda`, `agotado`, `desconocido`.

> Ojo con `solo_en_tienda`: **55 de los 80** productos Pokémon de weplay son retiro en tienda y
> no se pueden comprar online. Que el producto aparezca no significa que lo puedas comprar; lo
> que importa es que el botón *Agregar al Carro* quede habilitado. El monitor avisa de eso aparte.

**Avisa solo cuando cambia algo**: producto nuevo, paso a stock/preventa, botón de carrito
habilitado, o cambio de precio. El estado vive en `state.json`.

### Sobre robots.txt

`robots.txt` de weplay tiene `Disallow: /*?*product_list_`, así que el monitor **no** usa
`product_list_order=created_at` ni ningún parámetro `product_list_*`. Sí usa `brand_id` y
`catalogsearch`, que están permitidos.

GraphQL (`/graphql`) devuelve 403 y el sitemap está congelado en noviembre de 2025 — ninguno
de los dos sirve como fuente.

## Instalación

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Edita `.env` y pon tu topic de ntfy (uno largo y difícil de adivinar — cualquiera que lo sepa
puede leer tus avisos). Instala la app [ntfy](https://ntfy.sh) en el celular y suscríbete al
mismo topic. Telegram es opcional.

Comprueba que los avisos llegan:

```bash
python monitor.py --test
```

## Uso

```bash
python monitor.py
```

Queda corriendo y revisa cada 10 minutos. Otras opciones:

```bash
python monitor.py --once            # un solo ciclo (para cron / Programador de tareas)
python monitor.py --interval 15     # cada 15 minutos
python monitor.py --once -v         # con detalle en consola
```

El log va a `monitor.log` con rotación (5 archivos de 1 MB).

## Dejarlo corriendo

### Windows — Programador de tareas

```powershell
schtasks /create /tn "Monitor UPC Pokemon" /sc minute /mo 10 /tr "pythonw \"C:\Users\xGabo\Documents\Pokemon UPC\monitor.py\" --once" /st 09:00
```

`pythonw` evita que aparezca una ventana negra cada 10 minutos. Para ver o quitar la tarea:

```powershell
schtasks /query /tn "Monitor UPC Pokemon"
```

```powershell
schtasks /delete /tn "Monitor UPC Pokemon" /f
```

### Linux / macOS — cron

`crontab -e` y agrega:

```
*/10 * * * * cd /ruta/al/proyecto && /usr/bin/python3 monitor.py --once >> cron.log 2>&1
```

Cron no hereda tu entorno, así que usa rutas absolutas. El `.env` se lee desde el directorio
del script gracias al `cd`.

### GitHub Actions — cada 15 minutos

Ya está en `.github/workflows/monitor.yml`. Sube el repo y en
**Settings → Secrets and variables → Actions** crea:

- `NTFY_TOPIC` (obligatorio)
- `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (opcionales)

El workflow commitea `state.json` de vuelta al repo para no repetir avisos entre ejecuciones,
y guarda `monitor.log` como artefacto por 7 días.

Dos notas honestas:

1. **weplay.cl filtra por User-Agent, no por IP.** Comprobado el 17 de septiembre de 2026 desde
   una misma conexión: `python-requests`, `curl` y `Googlebot` reciben **403**; Chrome recibe
   **200**. El monitor ya se identifica como Chrome, así que debería funcionar desde un runner.
   Lo que queda sin comprobar es si Cloudflare bloquea además las IP de centros de datos — eso
   solo se sabe ejecutando el workflow una vez con *Run workflow*. Hazlo antes de confiar en
   esta vía; si da 403, corre el monitor en tu PC.
2. **El cron de GitHub no es puntual.** En repos públicos los trabajos se encolan y pueden
   atrasarse bastante en horas de carga. Es fiable para vigilar durante semanas, no para pelear
   un lanzamiento que se agota en 60 segundos.

Si el repo es privado, además consume minutos de Actions.

## Opciones extra

En `.env`:

- `PROBE_PRODUCT_URLS=true` — sondea las 6 URLs de producto más probables (`+6` peticiones por
  ciclo). Solo sirve si temes que lo publiquen sin indexarlo en la búsqueda. Las 32 variantes
  que probé daban 404 al 16 de septiembre de 2026.
- `PLAYWRIGHT_FALLBACK=true` — abre la ficha con un navegador real cuando el HTML plano no
  revela el stock. Requiere `pip install playwright && playwright install chromium`. Con las
  fuentes actuales **no hace falta**: la búsqueda y el listado ya traen el stock.

## Sobre el código de barras

No lo uso. No hay UPC/EAN público para Day/Night, y aunque lo hubiera no serviría: weplay
indexa por código interno propio (`1962141462420`, `1962141588011`…), que no es predecible.

## Limitaciones

- Si weplay publica el producto con un nombre que no contiene "ultra premium" (por ejemplo
  "Colección Ultra Premium" abreviada de otra forma), la regla no lo va a pillar. El listado de
  marca Pokémon es el respaldo, pero tendrías que revisarlo a ojo.
- El monitor detecta y avisa; no compra nada.
