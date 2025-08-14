create table if not exists users(
  telegram_id bigint primary key,
  tz text not null default 'Atlantic/Madeira',
  send_time text not null default '08:00',
  locale text not null default 'en'
);

create table if not exists decks(
  id serial primary key,
  unit text unique not null,
  title text not null,
  quizlet_url text not null,
  archived boolean not null default false
);

create table if not exists user_decks(
  user_id bigint not null,
  deck_id int not null references decks(id) on delete cascade,
  active boolean not null default true,
  -- FSRS (deck-level) state:
  difficulty double precision not null default 0.3, -- 0..1 (ниже = проще)
  stability   double precision not null default 1.0, -- дни
  next_due date,
  primary key(user_id, deck_id)
);

create table if not exists events(
  id bigserial primary key,
  user_id bigint not null,
  deck_id int not null,
  ts timestamptz not null default now(),
  action text not null check (action in ('worked','abit','didnt'))
);

create index if not exists idx_user_decks_due on user_decks(user_id, next_due);
