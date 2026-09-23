# Perfiles de empresas

Cada archivo `.json` de esta carpeta es una empresa. El radar genera una página para cada una.

```json
{
  "nombre": "Nombre que aparece en la página",
  "carpeta": "z-a1b2c3d4",          // "" = página principal; otro texto = subcarpeta (dirección privada)
  "regiones": [11],                  // 1..16 (11 = Aysén, 13 = Metropolitana, etc.)
  "rubros": { "72102200": "Servicios eléctricos" },   // códigos de 8 dígitos de Mercado Público
  "palabras": ["electricidad", "cableado"],            // detectan compras mal clasificadas (POSIBLE)
  "excluir": ["vigilancia"],                          // si aparecen, la compra no se marca como POSIBLE
  "activo": true                                       // false = pausar esta empresa sin borrarla
}
```

Dirección de la página: `https://<usuario>.github.io/radar-aysen/<carpeta>/`

Para buscar el código de un rubro por su nombre:
https://www.mercadopublico.cl/portal/Modules/Site/Search/SearchRubros.aspx
