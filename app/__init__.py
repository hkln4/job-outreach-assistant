import warnings

from bs4 import XMLParsedAsHTMLWarning

# Sitemaps and some odd homepages are read with the HTML parser on purpose
# (it finds what we need without lxml's XML mode). Don't flood the console.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
