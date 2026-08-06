-- Run this in your Supabase project's SQL Editor (Project -> SQL Editor -> New query)

create table if not exists products (
  id text primary key,
  name text not null,
  emoji text default '📦',
  tint text default '#FFF8F1',
  image_url text,
  price integer not null,
  old_price integer,
  discount integer default 0,
  rating numeric default 4.5,
  reviews text default '0',
  stock text default 'in' check (stock in ('in','low','out')),
  category text,
  created_at timestamptz default now()
);

-- If you already ran this script before image_url existed, run this
-- separately in the SQL Editor to add the column without losing your data:
-- alter table products add column if not exists image_url text;

create table if not exists orders (
  id bigint generated always as identity primary key,
  order_ref text unique not null,
  customer_name text not null,
  customer_mobile text not null,
  customer_address text not null,
  items jsonb not null,
  total integer not null,
  type text default 'cart_checkout',
  status text default 'pending' check (status in ('pending','confirmed','delivered','cancelled')),
  placed_at timestamptz default now()
);

create table if not exists settings (
  key text primary key,
  value text
);

-- Seed settings (banner + storefront config the admin can edit)
insert into settings (key, value) values
  ('banner_eyebrow', 'Student Life, Made Easy'),
  ('banner_title', 'Everything You Need, All in One Place.'),
  ('banner_subtitle', 'Best Quality • Best Prices • Made for Students'),
  ('banner_discount', '20'),
  ('free_delivery_threshold', '500')
on conflict (key) do nothing;

-- Seed products (same demo data as the frontend, now backed by the database)
insert into products (id, name, emoji, tint, price, old_price, discount, rating, reviews, stock, category) values
  ('p1', 'boAt Airdopes 141', '🎧', '#F1EEFB', 1199, 1499, 20, 4.4, '1.2k', 'in', 'Electronics'),
  ('p2', 'Fire-Boltt Phoenix Pro', '⌚', '#FDEFE6', 1699, 1999, 16, 4.5, '880', 'low', 'Electronics'),
  ('p3', 'Men''s Hoodie — Charcoal', '🧥', '#ECEFF5', 749, 999, 25, 4.3, '560', 'in', 'Fashion'),
  ('p4', 'Ambrane 10000mAh Power Bank', '🔋', '#EAF3EC', 899, 1099, 16, 4.4, '780', 'in', 'Electronics'),
  ('p5', 'Skybags Brat Backpack', '🎒', '#F5EDE6', 1169, 1499, 22, 4.4, '760', 'out', 'Fashion'),
  ('p6', 'Noise ColorFit Pulse 3', '⌚', '#FDEFE6', 1299, 1599, 19, 4.4, '640', 'in', 'Electronics'),
  ('p7', 'Campus Sneakers', '👟', '#ECEFF5', 1799, 2199, 18, 4.3, '430', 'in', 'Fashion'),
  ('p8', 'Wild Stone Perfume', '🧴', '#EAF3EC', 599, 799, 25, 4.5, '920', 'low', 'Grooming'),
  ('p9', 'Realme Buds T300', '🎧', '#F1EEFB', 1399, 1699, 18, 4.3, '510', 'in', 'Electronics'),
  ('p10', 'HRX Gym Bag', '🎒', '#F5EDE6', 899, 1099, 18, 4.2, '310', 'in', 'Sports')
on conflict (id) do nothing;
