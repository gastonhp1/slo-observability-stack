"""slogen: SLO specs (Sloth ``prometheus/v1`` format) -> Prometheus rules.

Implements the multiwindow, multi-burn-rate alerting from the Google SRE
Workbook, chapter "Alerting on SLOs":

=========  ============  ==========  ===========  ============================
Severity   Long window   Short win.  Burn rate    Budget consumed when it fires
=========  ============  ==========  ===========  ============================
page       1h            5m          14.4x        2% of a 30d budget
page       6h            30m         6x           5%
ticket     1d            2h          3x           10%
ticket     3d            6h          1x           10%
=========  ============  ==========  ===========  ============================
"""

__version__ = "0.1.0"
