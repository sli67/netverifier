-- Agent messages only: when the user's message reads as something the staged
-- workflow should handle, what the dashboard offers to start.
-- { "mode": "verify" | "change", "intent": "..." }. Null for plain questions.
alter table messages add column suggestion jsonb;
