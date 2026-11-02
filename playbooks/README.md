# Playbooks

Un playbook es un flujo de negocio que Jarvis sabe trabajar: instrucciones, herramientas propias con su
efecto declarado y límites de gasto. Se enchufa sin tocar el núcleo y puede vivir en otra carpeta (un repo
privado, por ejemplo) con `playbooks_dir` en `jarvis.yaml`:

```yaml
playbooks_dir: ../jarvis-private/playbooks
playbooks:
  mi_negocio:
    enabled: true
    settings:
      sender_name: Ana
      sender_identity: "Ana García · NIF 00000000T · Madrid"   # tus datos, aquí y no en el repo
```

## Crear uno nuevo

1. Copia `_plantilla/` como `<carpeta de playbooks>/mi_negocio/` (minúsculas y guiones bajos). La carpeta
   de playbooks necesita un `__init__.py` vacío.
2. `playbook.yaml`: título, descripción, claves que puede usar (`secrets`), ajustes y límites.
3. `instructions.md`: cómo debe trabajar Jarvis ese negocio, en lenguaje normal.
4. `tools.py`: sus herramientas. **Cada una declara su efecto** en `SPECS` (`READ`, `WRITE` o `IRREVERSIBLE`);
   si falta alguna, Jarvis no arranca el playbook. Todo lo que envíe, publique o cobre es `IRREVERSIBLE` y
   siempre espera tu ✅, diga lo que diga la configuración.
5. Añade tests y actívalo en `jarvis.yaml`.

Un playbook recibe solo lo que necesita (`PlaybookContext`): sus ajustes, la base de datos, una carpeta de
trabajo, avisar por Telegram, el modelo rápido, subagentes confinados a su carpeta y las claves que declare.
Jarvis no puede modificar la carpeta de playbooks.
