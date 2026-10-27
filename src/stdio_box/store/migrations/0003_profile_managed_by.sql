-- Set to 'credsctl sync' when that command writes a profile.
-- `credsctl sync --prune` deletes only profiles with this mark.
ALTER TABLE profiles ADD COLUMN managed_by TEXT;
