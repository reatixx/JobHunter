# JobHunter

**Mi asistente de búsqueda de empleo en España, con agentes de IA.**

Buscar trabajo en España me llevaba horas: revisar varios portales, descartar ofertas que piden inglés C1
o coche propio, adaptar el CV y escribir una carta para cada una. JobHunter automatiza la parte repetitiva
y me deja solo lo que requiere decisión humana: revisar y enviar.

> Documentación detallada en ruso: [README.ru.md](README.ru.md).

## Qué hace

**1. Busca ofertas** en varias fuentes sin duplicados:
LinkedIn (vista pública), InfoJobs (API oficial), Jooble (API), Job Today, portales de empleo remoto
(Remotive, RemoteOK, Himalayas, Jobicy, Arbeitnow) y canales de Telegram. También se puede añadir una oferta a mano.

**2. Filtra sin IA** (rápido y gratis), con el motivo visible para cada oferta descartada:
- inglés obligatorio B2/C1 ("valorable" no cuenta);
- solo remoto o cerca de casa (sin mudanza);
- puestos de responsable o senior;
- prácticas no remuneradas;
- carné de conducir o vehículo propio obligatorio;
- empresas que no quiero considerar, palabras prohibidas.

**3. Evalúa** cada oferta con un modelo local (Ollama, `qwen2.5:14b`): % de encaje, requisitos, lo que falta, señales de alerta.

**4. Prepara la candidatura** para las mejores (Claude): CV adaptado a la oferta en PDF y DOCX,
optimizado para ATS, y carta de presentación en español.
El CV se genera a partir de una **base de hechos** sobre el candidato, no del CV antiguo:
así los agentes no inventan experiencia. Si falta un dato, el agente pregunta en vez de suponer.

**5. Avisa por Telegram** con cada candidatura lista: ficha, PDF y carta, con botones
"✅ Enviado", "🙈 Saltar" y "✏️ Corregir carta" (escribes qué cambiar y el agente la reescribe).

**6. Se ejecuta solo** con el Programador de tareas de Windows. Nunca envía nada sin mí.

## Agentes

| # | Agente | Qué hace | Backend |
|---|---|---|---|
| 1 | Análisis de CV | perfil, puntos fuertes y débiles, palabras clave | Claude |
| 2 | Análisis de ofertas | % de encaje, requisitos, lo que falta | Ollama (local) |
| 3 | Carta de presentación | carta en español adaptada a la oferta | Claude |
| 4 | Adaptación de CV | perfil y palabras clave para la oferta | Claude |
| 5 | Auditoría ATS | palabras clave reales de las ofertas, criterios eliminatorios | Claude |
| 6 | CV nuevo | reescribe el CV para ATS (solo hechos reales) → PDF y DOCX | Claude |

El PDF final se comprueba como lo leería un ATS: se extrae el texto de nuevo y se recalcula la cobertura de palabras clave.

## Tecnología

Python · Streamlit (interfaz) · SQLite · Pydantic (salidas estructuradas de los modelos) ·
Anthropic SDK (Claude) · Ollama · requests/BeautifulSoup · fpdf2 y python-docx · API de Telegram Bot ·
Programador de tareas de Windows.

## Puesta en marcha

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      :: claves: Claude, Jooble, InfoJobs, Telegram
run.bat                     :: abre la interfaz en http://localhost:8501
```

Autopiloto manual: `.venv\Scripts\python.exe -m jobhunter.pipeline [--skip-search] [--max-prepare N]`,
programación: `--install-schedule` / `--remove-schedule`.

La interfaz está en ruso (mi idioma nativo); los CV y las cartas se generan en español.

## Cómo lo hice

Proyecto hecho con **Claude Code** (desarrollo asistido por IA). Yo definí qué necesitaba, las reglas de filtrado
a partir de mi experiencia real buscando trabajo, y probé el sistema con ofertas reales; la IA escribió el código
siguiendo esas indicaciones. Con JobHunter preparé y envié candidaturas reales.
