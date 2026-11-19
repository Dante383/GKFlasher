# Bundled GUI fonts

Linux and other non-Windows platforms load DejaVu Sans Regular/Bold for controls
and DejaVu Sans Mono for logs and fixed-width inputs. Windows uses its native
UI font and installed Consolas font. No font installation or runtime download is required.

These files are redistributed unchanged from Matplotlib 3.11.2's
`matplotlib/mpl-data/fonts/ttf/` directory. The complete accompanying copyright
notices and redistribution terms are in `LICENSE_DEJAVU`; ship it with the fonts.

Sources: [DejaVu Fonts](https://dejavu-fonts.github.io/),
[Matplotlib font assets](https://github.com/matplotlib/matplotlib/tree/v3.11.2/lib/matplotlib/mpl-data/fonts/ttf).

SHA256:

- DejaVuSans.ttf: `3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22`
- DejaVuSans-Bold.ttf: `b184b89e3c1075f22f6b71575b6fc20d4972b3cfd3b23322ca6fd596dcaef167`
- DejaVuSansMono.ttf: `602ec86b8948cfcd956482fe64f94c36c867770149ef2f791d4613f443bcecb3`

The GUI packaging spec includes the complete assets directory.
