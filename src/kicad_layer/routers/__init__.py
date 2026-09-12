"""The routers: frozen.

Differential-pair routing (``pairrouter``), plane stitching (``stitch``), the clean-up router for
what an autorouter leaves open (``cleanup``), the FreeRouting pass through Specctra files (``dsn``,
``ses``, ``freerouting``), copper operations on saved routes (``copper``), a copper plot (``plot``)
the MCP entry points (``routing_tools``) and the pipeline a project's routing script drives
(``pipeline``: a ``RoutingPlan`` and the passes). Together about a quarter of the code base.

The intended loop routes by hand in KiCad and captures the copper as data (``kicad_layer.routes``),
so this package gets fixes, not features. Only ``kicad_layer.tools`` (the full tool tier) and a
project's routing script import it; ``tests/test_layers.py`` enforces that. Delete it when the
last board that needs it is retired.
"""
