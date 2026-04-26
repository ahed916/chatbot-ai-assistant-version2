import { createClient } from "@supabase/supabase-js";

// ONLY the anon key here — never the service role key
export const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_ANON_KEY
);