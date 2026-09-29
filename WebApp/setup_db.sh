#!/bin/bash

dotenvx run -f .env.prod -- python ./manage.py migrate
dotenvx run -f .env.prod -- python ./manage.py fetch_pos_models
dotenvx run -f .env.prod -- python ./manage.py loaddata languages academic_fields
dotenvx run -f .env.prod -- python ./manage.py loaddocuments ./test_documents/data
dotenvx run -f .env.prod -- python ./manage.py loaddocumentmetadata ./test_documents/metadata.json
dotenvx run -f .env.prod -- python ./manage.py backfill_selection_method