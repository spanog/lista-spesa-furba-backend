-- Branches use the official ISTAT Comune only. Exact branch addresses and
-- branch coordinates are no longer collected, stored, or used for discovery.

DROP TRIGGER IF EXISTS supermarkets_set_location ON public.supermarkets;
DROP FUNCTION IF EXISTS public.set_supermarket_location_from_lat_lng();
DROP INDEX IF EXISTS public.idx_supermarkets_location;

UPDATE public.supermarkets
SET is_active = false
WHERE municipality_code IS NULL
  AND is_active = true;

ALTER TABLE public.supermarkets
  DROP COLUMN IF EXISTS location,
  DROP COLUMN IF EXISTS lat,
  DROP COLUMN IF EXISTS lng,
  DROP COLUMN IF EXISTS address,
  DROP COLUMN IF EXISTS city,
  DROP COLUMN IF EXISTS province,
  DROP COLUMN IF EXISTS postal_code;

ALTER TABLE public.supermarkets
  DROP CONSTRAINT IF EXISTS supermarkets_active_municipality_code_check;

ALTER TABLE public.supermarkets
  ADD CONSTRAINT supermarkets_active_municipality_code_check
  CHECK (is_active = false OR municipality_code IS NOT NULL);

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
    ST_Distance(branch_municipality.center, selected.center) / 1000,
    supermarket.municipality_code = selected.code
  FROM public.supermarkets AS supermarket
  JOIN public.municipalities AS branch_municipality
    ON branch_municipality.code = supermarket.municipality_code
  CROSS JOIN selected
  WHERE supermarket.is_active = true
    AND radius_m > 0
    AND (
      supermarket.municipality_code = selected.code
      OR ST_DWithin(branch_municipality.center, selected.center, radius_m)
    )
  ORDER BY
    (supermarket.municipality_code = selected.code) DESC,
    ST_Distance(branch_municipality.center, selected.center),
    supermarket.name;
$$;

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
  JOIN public.municipalities AS selected_municipality
    ON selected_municipality.code = profile.municipality_code
  JOIN public.municipalities AS target_municipality
    ON target_municipality.code = target.municipality_code
  WHERE target.id = target_supermarket_id
    AND target.is_active = true
    AND profile.role = 'customer'
    AND profile.municipality_code IS NOT NULL
    AND (
      target.municipality_code = profile.municipality_code
      OR ST_DWithin(
        target_municipality.center,
        selected_municipality.center,
        COALESCE(profile.max_distance_km, 10)::DOUBLE PRECISION * 1000
      )
    );
$$;
