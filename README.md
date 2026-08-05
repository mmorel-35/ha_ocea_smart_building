# Ocea Smart Building - Home Assistant Integration

[![hacs][hacs-badge]][hacs-url]
[![release][release-badge]][release-url]
![downloads][downloads-badge]

Home Assistant integration to monitor water and heating consumption from the [Ocea Smart Building](https://espace-resident.ocea-sb.com) resident portal.

## Features

- **Cold water** consumption in m³
- **Hot water** consumption in m³
- **CETC heating** consumption in kWh when available for the dwelling
- Compatible with the **Energy dashboard** (water and energy sections)
- Automatic Azure AD B2C authentication (no headless browser needed)
- Automatic token refresh
- UI-based configuration
- Native Home Assistant reconfiguration for the email and password

## Installation

### Via HACS (recommended)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add the repository URL, category **Integration**
3. Search for "Ocea Smart Building" → Install
4. Restart Home Assistant

### Manual

Copy `custom_components/ocea_smart_building/` into `config/custom_components/` and restart Home Assistant.

## Configuration

Settings → Devices & Services → Add Integration → "Ocea Smart Building"

- **Email**: your Ocea resident portal email
- **Password**: your Ocea password

Your dwelling is automatically detected from your Ocea account.

### Reconfiguration

Open Settings → Devices & Services → Ocea Smart Building, then select the native **Reconfigure** action.

- The current email address is prefilled and can be changed.
- The stored password is never displayed. Leave the password field unchanged to keep it, or enter a new password to replace it.
- Credentials are validated with Ocea before changes are saved.
- Home Assistant reloads the integration only when the validated configuration has actually changed.

## Energy dashboard

Add the cold and hot water sensors in Settings → Dashboards → Energy → Water consumption. When available, the CETC heating sensor can also be added as an energy-consumption source.

## Troubleshooting

```yaml
logger:
  logs:
    custom_components.ocea_smart_building: debug
```

<!-- Badge references -->
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/Triskae/ha_ocea_smart_building
[release-url]: https://github.com/Triskae/ha_ocea_smart_building/releases
[downloads-badge]: https://img.shields.io/github/downloads/Triskae/ha_ocea_smart_building/total
