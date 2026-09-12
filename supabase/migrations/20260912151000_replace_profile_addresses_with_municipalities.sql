CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA extensions;

CREATE OR REPLACE FUNCTION public.normalize_municipality_text(value TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
SET search_path = public, extensions
AS $$
  SELECT regexp_replace(lower(unaccent(trim(COALESCE(value, '')))), '\\s+', ' ', 'g');
$$;

ALTER TABLE public.user_profiles
  ADD COLUMN IF NOT EXISTS municipality_code TEXT
    REFERENCES public.municipalities(code) ON DELETE RESTRICT;

ALTER TABLE public.supermarkets
  ADD COLUMN IF NOT EXISTS municipality_code TEXT
    REFERENCES public.municipalities(code) ON DELETE RESTRICT;

DROP POLICY IF EXISTS "supermarkets_read_all" ON public.supermarkets;

UPDATE public.user_profiles AS profile
SET municipality_code = municipality.code
FROM public.municipalities AS municipality
WHERE profile.municipality_code IS NULL
  AND municipality.normalized_name = public.normalize_municipality_text(profile.home_city)
  AND (
    municipality.province_code = upper(trim(profile.home_province))
    OR public.normalize_municipality_text(municipality.province_name)
      = public.normalize_municipality_text(profile.home_province)
  );

UPDATE public.supermarkets AS supermarket
SET municipality_code = municipality.code
FROM public.municipalities AS municipality
WHERE supermarket.municipality_code IS NULL
  AND municipality.normalized_name = public.normalize_municipality_text(supermarket.city)
  AND (
    municipality.province_code = upper(trim(supermarket.province))
    OR public.normalize_municipality_text(municipality.province_name)
      = public.normalize_municipality_text(supermarket.province)
  );

CREATE INDEX IF NOT EXISTS idx_user_profiles_municipality_code
  ON public.user_profiles(municipality_code);

CREATE INDEX IF NOT EXISTS idx_supermarkets_municipality_code
  ON public.supermarkets(municipality_code);

DROP TRIGGER IF EXISTS user_profiles_set_locations ON public.user_profiles;
DROP FUNCTION IF EXISTS public.set_profile_locations_from_lat_lng();
DROP INDEX IF EXISTS public.idx_user_profiles_home_location;
DROP INDEX IF EXISTS public.idx_user_profiles_search_location;

ALTER TABLE public.user_profiles
  DROP COLUMN IF EXISTS home_location,
  DROP COLUMN IF EXISTS search_location,
  DROP COLUMN IF EXISTS home_address,
  DROP COLUMN IF EXISTS home_city,
  DROP COLUMN IF EXISTS home_province,
  DROP COLUMN IF EXISTS home_postal_code,
  DROP COLUMN IF EXISTS home_lat,
  DROP COLUMN IF EXISTS home_lng,
  DROP COLUMN IF EXISTS search_label,
  DROP COLUMN IF EXISTS search_lat,
  DROP COLUMN IF EXISTS search_lng;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  created_list_id UUID;
BEGIN
  INSERT INTO public.user_profiles (
    id, display_name, role, managed_supermarket_id
  ) VALUES (
    NEW.id,
    COALESCE(NEW.raw_user_meta_data->>'display_name', split_part(NEW.email, '@', 1)),
    'customer',
    NULL
  ) ON CONFLICT (id) DO NOTHING;

  SELECT shopping_lists.id INTO created_list_id
  FROM public.shopping_lists
  WHERE shopping_lists.user_id = NEW.id
  ORDER BY shopping_lists.created_at ASC NULLS LAST, shopping_lists.id ASC
  LIMIT 1;

  IF created_list_id IS NULL THEN
    INSERT INTO public.shopping_lists (user_id, name, items, is_active)
    VALUES (NEW.id, 'La mia lista', '[]'::jsonb, true)
    RETURNING id INTO created_list_id;
  END IF;

  INSERT INTO public.list_members (list_id, user_id, role)
  VALUES (created_list_id, NEW.id, 'owner')
  ON CONFLICT (list_id, user_id) DO NOTHING;

  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.visible_supermarkets_for_municipality(
  selected_municipality_code TEXT,
  radius_m DOUBLE PRECISION DEFAULT 10000
)
RETURNS TABLE(
  id UUID,
  distance_km DOUBLE PRECISION,
  is_selected_municipality BOOLEAN
)
LANGUAGE sql
STABLE
SET search_path = public, extensions
AS $$
  WITH selected AS (
    SELECT code, center
    FROM public.municipalities
    WHERE code = selected_municipality_code
  )
  SELECT
    supermarket.id,
    CASE
      WHEN supermarket.location IS NULL THEN NULL
      ELSE ST_Distance(supermarket.location, selected.center) / 1000
    END,
    supermarket.municipality_code = selected.code
  FROM public.supermarkets AS supermarket
  CROSS JOIN selected
  WHERE supermarket.is_active = true
    AND radius_m > 0
    AND (
      supermarket.municipality_code = selected.code
      OR (
        supermarket.location IS NOT NULL
        AND ST_DWithin(supermarket.location, selected.center, radius_m)
      )
    )
  ORDER BY
    (supermarket.municipality_code = selected.code) DESC,
    ST_Distance(supermarket.location, selected.center) NULLS LAST,
    supermarket.name;
$$;

REVOKE ALL ON FUNCTION public.visible_supermarkets_for_municipality(TEXT, DOUBLE PRECISION)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.visible_supermarkets_for_municipality(TEXT, DOUBLE PRECISION)
  TO service_role;

CREATE OR REPLACE FUNCTION public.flyer_notification_recipients(
  target_supermarket_id UUID
)
RETURNS TABLE(user_id UUID)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, extensions
AS $$
  SELECT profile.id
  FROM public.user_profiles AS profile
  WHERE profile.role = 'admin'

  UNION

  SELECT profile.id
  FROM public.user_profiles AS profile
  WHERE profile.role = 'supermarket_manager'
    AND profile.managed_supermarket_id = target_supermarket_id

  UNION

  SELECT profile.id
  FROM public.user_profiles AS profile
  CROSS JOIN public.supermarkets AS target
  JOIN public.municipalities AS municipality
    ON municipality.code = profile.municipality_code
  WHERE target.id = target_supermarket_id
    AND target.is_active = true
    AND profile.role = 'customer'
    AND profile.municipality_code IS NOT NULL
    AND (
      target.municipality_code = profile.municipality_code
      OR (
        target.location IS NOT NULL
        AND ST_DWithin(
          target.location,
          municipality.center,
          COALESCE(profile.max_distance_km, 10)::DOUBLE PRECISION * 1000
        )
      )
    );
$$;

REVOKE ALL ON FUNCTION public.flyer_notification_recipients(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.flyer_notification_recipients(UUID) TO service_role;
