TTE PROJECT B WEATHER TRAINING LINEAGE CLOSURE v1.0.0

Purpose
-------
Read-only discovery of the metadata-referenced cleaned Pedro and TRI daily
weather artifacts, the TRI June supplement, legacy caches, builder-rule
references, and possible code consumers.

This package does not modify or copy source evidence. It does not train a
model, modify Project A or Project B, authorize weather features, or execute
RL allocation.

Expected cleaned artifacts
--------------------------
tri_weather_daily.csv.gz
  SHA-256 d489af0aac8dbdb86ca38c8ea20565024bf7c132998835380732572ca8947d9f

pedro_weather_daily.csv.gz
  SHA-256 28be35ce5896b2837bf12c35e3db315b8bead666df3fdce263831a8eb9694a58

Required sequence
-----------------
1. Run RUN_SELF_TEST.ps1.
2. Run RUN_PROJECT_B_WEATHER_TRAINING_LINEAGE_CLOSURE.ps1.
3. Upload the automatically created
   Project_B_Weather_Training_Lineage_Closure_Review_*.zip.

Governance controls
-------------------
PROJECT_B_RETRAINING=UNAUTHORIZED
RL_ALLOCATION=DISABLED
