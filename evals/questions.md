# Evals — 10 realistic read-only questions

Acceptance protocol for "fonctionnement irréprochable": ask each question to an
agent connected to this server, then verify the answer against the MyToyota
mobile app (same account, refreshed). The agent must answer in **one tool
call**, with explicit units, and cite data age when the question is about
current state.

| # | Question (FR / EN) | Expected tool | Fields used | Verification |
|---|---|---|---|---|
| 1 | Il reste combien d'autonomie ? / How much range is left? | `toyota_get_energy` | `total_range`, `fuel_level_percent` | App home screen range |
| 2 | Elle est où, Tino ? / Where is the car? | `toyota_get_location` | `latitude`, `longitude`, `google_maps_url`, `freshness.vehicle_reported_at` | App map pin |
| 3 | La voiture est verrouillée ? / Is the car locked? | `toyota_get_status` | `all_locked`, `freshness.vehicle_reported_at` | App vehicle status |
| 4 | Une fenêtre est restée ouverte ? / Any window left open? | `toyota_get_status` | `windows.*` | App vehicle status |
| 5 | Combien de km au compteur ? / What's the odometer? | `toyota_get_odometer` | `odometer` | App odometer |
| 6 | Conso du dernier trajet ? / Last trip's consumption? | `toyota_get_last_trip` | `average_consumption`, `distance`, `ev_ratio_percent` | App last trip card |
| 7 | Conso moyenne des 7 derniers jours ? / Average consumption last 7 days? | `toyota_get_trip_summary` (days=7) | `average_consumption`, `total_distance` | App weekly stats (approx — app uses calendar weeks) |
| 8 | Quel est mon ratio EV ce mois-ci ? / EV share this month? | `toyota_get_trip_summary` (days=30) | `ev_ratio_percent`, `ev_time_ratio_percent` | App hybrid coaching screen |
| 9 | Des alertes sur la voiture ? / Any alerts on the car? | `toyota_get_health` | `warning_lights`, `notifications` | App notifications |
| 10 | Liste mes trajets de la semaine. / List this week's trips. | `toyota_get_trips` (days=7) | `trips[]`, `returned_count` | App trip list |

Expected failure behaviors (also part of the acceptance bar):

- Battery question on Tino (full hybrid) → the agent explains "not applicable
  to this powertrain", it does **not** invent a battery percentage.
- Toyota API down → the agent serves the last snapshot and says how old it is.
- Wrong credentials → the agent relays the sign-in failure with the doctor hint,
  and the server does not hammer the login endpoint (60 s cooldown).
