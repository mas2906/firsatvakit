-- deals tablosuna kupon kolonu
ALTER TABLE deals ADD COLUMN IF NOT EXISTS coupon TEXT;

-- products tablosuna varyant JSON kolonu
ALTER TABLE products ADD COLUMN IF NOT EXISTS variants TEXT;
