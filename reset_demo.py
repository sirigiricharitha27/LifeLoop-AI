"""Reset the demo database (fresh simulated events for today)."""
from lifeloop import db
db.init(reset=True); print("Demo data reset.")
