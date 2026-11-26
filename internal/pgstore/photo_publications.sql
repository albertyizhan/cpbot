ALTER TABLE cpbot.publications ADD COLUMN photo boolean NOT NULL DEFAULT false;
ALTER TABLE cpbot.jobs ADD COLUMN attempt_photo boolean NOT NULL DEFAULT false;
