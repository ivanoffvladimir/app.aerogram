import { Card, Col, Row, Statistic, Typography, Empty } from 'antd'

/**
 * Дашборд (раздел 13 ТЗ): отправления в пути, риск срыва срока, доставлено за месяц,
 * средний срок, топ проблем.
 *
 * Пока адаптеры не подключены, показатели пустые: подставлять правдоподобные числа
 * вместо отсутствующих данных нельзя — на этом же принципе построен Carrier Score
 * (раздел 10.2 ТЗ).
 */
export function DashboardPage() {
  return (
    <>
      <Typography.Title level={3}>Дашборд</Typography.Title>
      <Row gutter={16}>
        <Col span={6}>
          <Card>
            <Statistic title="В пути" value="—" />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="Риск срыва срока" value="—" />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="Доставлено за месяц" value="—" />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="Средний срок, дней" value="—" />
          </Card>
        </Col>
      </Row>
      <Card style={{ marginTop: 16 }} title="Топ проблем">
        <Empty description="Данные появятся после подключения первого перевозчика" />
      </Card>
    </>
  )
}
