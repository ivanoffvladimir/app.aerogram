import { Table, Typography, Empty } from 'antd'
import type { ColumnsType } from 'antd/es/table'

interface ShipmentRow {
  id: string
  number: string
  carrier: string
  status: string
  recipient: string
  createdAt: string
}

/** Колонки списка отправлений (раздел 13 ТЗ). */
const columns: ColumnsType<ShipmentRow> = [
  { title: 'Номер', dataIndex: 'number', key: 'number', width: 140 },
  { title: 'Перевозчик', dataIndex: 'carrier', key: 'carrier', width: 160 },
  { title: 'Статус', dataIndex: 'status', key: 'status', width: 180 },
  { title: 'Получатель', dataIndex: 'recipient', key: 'recipient' },
  { title: 'Создано', dataIndex: 'createdAt', key: 'createdAt', width: 180 },
]

export function ShipmentsPage() {
  return (
    <>
      <Typography.Title level={3}>Отправления</Typography.Title>
      <Table<ShipmentRow>
        columns={columns}
        dataSource={[]}
        rowKey="id"
        locale={{
          emptyText: <Empty description="Отправлений пока нет" />,
        }}
      />
    </>
  )
}
