import cianparser

spb_parser = cianparser.CianParser(location="Санкт-Петербург")
data = spb_parser.get_flats(deal_type="sale", rooms=(1, 2, 3, 4, 5), with_saving_csv=True)

print(data[0])